"""Runtime configuration, loaded from a JSON file (see config.example.json)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    # Root of the new, structured dataset store (NAS mount).
    storage_root: str = "./data/storage"
    # SQLite catalog; keep it next to the storage so it travels with the NAS.
    db_path: str = "./data/catalog.sqlite"
    # Relative folder layout below storage_root. Placeholders are schema field
    # names (slugified); {dataset_id} keeps names unique for same-day recordings.
    path_template: str = "{customer}/{project}/{robot}/{date}_{site}_{robot_task}_{dataset_id}"
    # "copy" keeps the original data untouched (NAS decision: keep originals for now).
    ingest_mode: str = "copy"  # copy | move | register (index in place, no copy)
    # Optional marina integration: push every ingested dataset to this registry.
    marina_registry: Optional[str] = None
    marina_bin: str = "marina"
    # Viewer for the "Open in Foxglove" preview. Also a self-hosted Lichtblick,
    # e.g. "http://127.0.0.1:8081/". Only this origin may fetch files via /files/.
    viewer_url: str = "https://app.foxglove.dev/~/view"
    # Folders the upload UI is allowed to browse for source data.
    browse_roots: tuple = (str(Path.home()),)
    host: str = "127.0.0.1"
    port: int = 8080

    @property
    def storage(self) -> Path:
        return Path(self.storage_root).expanduser().resolve()

    @property
    def db(self) -> Path:
        return Path(self.db_path).expanduser().resolve()


def load(path: Optional[str] = None) -> Config:
    path = path or os.environ.get("DATASET_HUB_CONFIG")
    cfg = Config()
    if path:
        base = Path(path).expanduser().resolve().parent
        raw = json.loads(Path(path).expanduser().read_text())
        known = {f.name for f in fields(Config)}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if k not in known:
                raise ValueError(f"unknown config key: {k}")
            if k == "browse_roots":
                v = tuple(v)
            setattr(cfg, k, v)
        # Relative paths in the config file are relative to the file itself.
        for k in ("storage_root", "db_path"):
            p = Path(getattr(cfg, k)).expanduser()
            if not p.is_absolute():
                setattr(cfg, k, str(base / p))
        cfg.browse_roots = tuple(
            str(Path(r).expanduser() if Path(r).expanduser().is_absolute() else base / r)
            for r in cfg.browse_roots
        )
    return cfg
