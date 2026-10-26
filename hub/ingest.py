"""Upload pipeline: validate -> inspect -> place in folder structure -> catalog.

Every stored dataset folder gets a `dataset.json` sidecar with its full
metadata, so the catalog can always be rebuilt from the storage alone
(`python -m hub reindex`).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import bag_info
from .config import Config
from .db import Catalog, new_id
from .marina_backend import Marina, marina_ref
from .schema import FIELD_BY_NAME, FIELDS, ValidationError, normalize, slug

SIDECAR = "dataset.json"


def target_path(cfg: Config, meta: Dict[str, Any], dataset_id: str) -> Path:
    values = {f.name: slug(str(meta.get(f.name))) if meta.get(f.name) is not None else "na"
              for f in FIELDS}
    values["dataset_id"] = dataset_id
    rel = cfg.path_template.format(**values)
    return cfg.storage / rel


def write_sidecar(path: Path, record: Dict[str, Any]) -> None:
    keep = {k: v for k, v in record.items() if k not in ("topics",)}
    (path / SIDECAR).write_text(json.dumps(keep, indent=2, sort_keys=True, default=str) + "\n")


def suggest_metadata(source: Path) -> Dict[str, Any]:
    """Auto-detected values to prefill the upload form."""
    info = bag_info.inspect(source)
    sidecar = source / SIDECAR
    prefill = {k: info.get(k) for k in ("date", "length_s", "has_gps")}
    if sidecar.exists():
        prefill.update({k: v for k, v in json.loads(sidecar.read_text()).items()
                        if k in FIELD_BY_NAME})
    return {"prefill": prefill, "info": info}


def ingest(cfg: Config, catalog: Catalog, source: Path, meta: Dict[str, Any],
           mode: Optional[str] = None, marina: Optional[Marina] = None) -> Dict[str, Any]:
    source = Path(source).expanduser().resolve()
    mode = mode or cfg.ingest_mode
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")
    if mode not in ("copy", "move", "register"):
        raise ValueError(f"unknown mode {mode}")

    info = bag_info.inspect(source)
    # Auto-detected values fill gaps but never override what the user entered.
    for k in ("date", "length_s", "has_gps"):
        if meta.get(k) in (None, "") and info.get(k) is not None:
            meta[k] = info[k]
    meta = normalize(meta)
    if info["bag_count"] == 0:
        raise ValidationError({"source": "no rosbag2/mcap/db3/bag found in folder"})

    dataset_id = new_id()
    if mode == "register":
        dest = source
    else:
        dest = target_path(cfg, meta, dataset_id)
        if dest.exists():
            raise ValueError(f"target already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copytree(source, dest)
        else:
            shutil.move(str(source), str(dest))

    system = {
        "dataset_id": dataset_id,
        "path": str(dest),
        "source_path": str(source),
        "size_bytes": info["size_bytes"],
        "file_count": info["file_count"],
        "storage_format": info["storage_format"],
        "topics": info["topics"],
    }
    try:
        catalog.insert(meta, system)
    except Exception:
        if mode == "copy":
            shutil.rmtree(dest, ignore_errors=True)
        elif mode == "move":
            shutil.move(str(dest), str(source))
        raise

    if marina is not None:
        return marina_sync(catalog, marina, dataset_id)
    record = catalog.get(dataset_id)
    write_sidecar(dest, record)
    return record


def marina_sync(catalog: Catalog, marina: Marina, dataset_id: str,
                force: bool = False) -> Dict[str, Any]:
    """Push a dataset under the ref derived from its current metadata.

    Skips datasets already pushed under that ref (unless force). If the ref
    changed (metadata edited), the old ref is removed from the registry.
    Failures don't raise: the dataset gets status=marina_push_failed and can
    be retried with `python -m hub marina-sync`.
    """
    rec = catalog.get(dataset_id)
    if rec is None:
        raise KeyError(f"unknown dataset {dataset_id}")
    ref = marina_ref(rec, dataset_id)
    old = rec.get("marina_ref")
    if old == ref and rec["status"] != "marina_push_failed" and not force:
        return rec
    path = Path(rec["path"])
    # Sidecar first, so the pushed copy carries its metadata and own ref.
    write_sidecar(path, {**rec, "marina_ref": ref})
    try:
        marina.push(ref, path)
    except Exception as e:  # catalog entry stays; push can be retried
        catalog.update(dataset_id, {}, {"status": "marina_push_failed"})
        print(f"warning: {e}")
    else:
        status = "ok" if rec["status"] == "marina_push_failed" else rec["status"]
        catalog.update(dataset_id, {}, {"marina_ref": ref, "status": status})
        if old and old != ref:
            try:
                marina.remove(old)
            except Exception as e:
                print(f"warning: pushed {ref} but could not remove old ref {old}: {e}")
    rec = catalog.get(dataset_id)
    write_sidecar(path, rec)
    return rec


def marina_pending(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Datasets whose registry state doesn't match the catalog (never pushed, failed, stale ref)."""
    return [r for r in records
            if r["status"] == "marina_push_failed"
            or r.get("marina_ref") != marina_ref(r, r["dataset_id"])]


def bulk_update(cfg: Config, catalog: Catalog, dataset_ids: List[str],
                changes: Dict[str, Any], relocate: bool = True,
                marina: Optional[Marina] = None) -> List[Dict[str, Any]]:
    """Set the same metadata on many datasets.

    If a path field changes and the dataset lives inside storage_root, the
    folder is moved so the structure stays consistent. With marina, datasets
    whose ref changed are pushed under the new ref and the old one is removed.
    """
    changes = normalize(changes, partial=True)
    if not changes:
        return []
    updated = []
    for did in dataset_ids:
        rec = catalog.get(did)
        if rec is None:
            raise KeyError(f"unknown dataset {did}")
        merged = {**{f.name: rec.get(f.name) for f in FIELDS}, **changes}
        system: Dict[str, Any] = {}
        old = Path(rec["path"])
        if relocate and _is_within(old, cfg.storage):
            new = target_path(cfg, merged, did)
            if new != old:
                if new.exists():
                    raise ValueError(f"target already exists: {new}")
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                _prune_empty_parents(old.parent, cfg.storage)
                system["path"] = str(new)
        catalog.update(did, changes, system)
        rec = catalog.get(did)
        if marina is not None and rec.get("marina_ref") and marina_pending([rec]):
            rec = marina_sync(catalog, marina, did)
        else:
            write_sidecar(Path(rec["path"]), rec)
        updated.append(rec)
    return updated


def reindex(cfg: Config, catalog: Catalog) -> int:
    """Rebuild catalog rows from dataset.json sidecars under storage_root."""
    n = 0
    for sidecar in cfg.storage.rglob(SIDECAR):
        data = json.loads(sidecar.read_text())
        did = data.get("dataset_id")
        if not did or catalog.get(did):
            continue
        info = bag_info.inspect(sidecar.parent)
        meta = normalize({k: v for k, v in data.items() if k in FIELD_BY_NAME})
        catalog.insert(meta, {**info, "dataset_id": did, "path": str(sidecar.parent),
                              "marina_ref": data.get("marina_ref")})
        n += 1
    return n


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _prune_empty_parents(path: Path, root: Path) -> None:
    while path != root and _is_within(path, root):
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent
