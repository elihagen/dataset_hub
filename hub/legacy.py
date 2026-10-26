"""Migration of existing NAS datasets into the new structure.

Step 1 `scan`:   walk the old NAS tree, find every bag, write a CSV with all
                 auto-detected metadata plus guesses from the folder names.
Step 2 (human):  review/complete the CSV in a spreadsheet. Bulk-fill columns
                 for a subset of rows (e.g. all rows below /nas/customerX/).
Step 3 `import`: ingest every row that is marked ready. Originals stay
                 untouched with the default copy mode.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import bag_info
from .config import Config
from .db import Catalog
from .ingest import ingest
from .marina_backend import Marina
from .schema import FIELDS, ValidationError, normalize

CSV_COLUMNS = ["import", "source_path", "size_gb", "bag_count", "storage_format",
               "topic_count", "gps_topics"] + [f.name for f in FIELDS]

_DATE_RE = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")


def _guess_date(path: Path) -> Optional[str]:
    for part in reversed(path.parts):
        m = _DATE_RE.search(part)
        if m:
            y, mo, d = m.groups()
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
    return None


def dataset_roots(root: Path) -> List[Path]:
    """One dataset per rosbag2 folder; loose bag files group by their parent folder."""
    roots = []
    for bag in bag_info.find_bags(root):
        roots.append(bag if bag.is_dir() else bag.parent)
    return sorted(set(roots))


def scan(root: Path, out_csv: Path, defaults: Optional[Dict[str, str]] = None) -> int:
    root = Path(root).expanduser().resolve()
    rows = []
    for ds in dataset_roots(root):
        info = bag_info.inspect(ds)
        rel = ds.relative_to(root).parts
        row: Dict[str, Any] = {c: "" for c in CSV_COLUMNS}
        row.update(defaults or {})
        row.update({
            "import": "no",
            "source_path": str(ds),
            "size_gb": round(info["size_bytes"] / 1e9, 3),
            "bag_count": info["bag_count"],
            "storage_format": info["storage_format"] or "",
            "topic_count": len(info["topics"]),
            "gps_topics": " ".join(t["name"] for t in info["topics"]
                                   if t["type"] in bag_info.GPS_TYPES),
            "date": info["date"] or _guess_date(ds) or "",
            "length_s": info["length_s"] if info["length_s"] is not None else "",
            "has_gps": "" if info["has_gps"] is None else str(info["has_gps"]).lower(),
            # The old NAS layout is unknown/inconsistent: offer the folder path
            # as a hint, the reviewer moves the parts into the right columns.
            "comment": "legacy: " + "/".join(rel),
        })
        rows.append(row)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def import_csv(cfg: Config, catalog: Catalog, csv_path: Path, mode: Optional[str] = None,
               marina: Optional[Marina] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Ingest all rows with import=yes. Returns {"ok": [...], "errors": [...]}."""
    result: Dict[str, List[Any]] = {"ok": [], "errors": []}
    with Path(csv_path).open(newline="") as f:
        rows = list(csv.DictReader(f))
    field_names = {f.name for f in FIELDS}
    for n, row in enumerate(rows, start=2):  # line 1 is the header
        if row.get("import", "").strip().lower() not in ("yes", "y", "true", "1"):
            continue
        meta = {k: v for k, v in row.items() if k in field_names and v not in (None, "")}
        src = row["source_path"]
        if catalog.find_by_source(str(Path(src).expanduser().resolve())):
            result["errors"].append({"line": n, "source_path": src, "error": "already imported"})
            continue
        try:
            if dry_run:
                normalize(meta)
                result["ok"].append({"line": n, "source_path": src})
            else:
                rec = ingest(cfg, catalog, Path(src), meta, mode=mode, marina=marina)
                result["ok"].append({"line": n, "source_path": src, "path": rec["path"]})
        except (ValidationError, ValueError, OSError) as e:
            result["errors"].append({"line": n, "source_path": src, "error": str(e)})
    return result
