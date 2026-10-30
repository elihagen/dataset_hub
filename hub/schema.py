"""Dataset metadata schema — the single source of truth for every field.

The UI form, the SQLite columns, validation, filters and the sidecar file
(`dataset.json`) are all derived from FIELDS, so adding a field means adding
one entry here.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"  # text | date | bool | float | list | json
    required: bool = False
    # Used to build the folder path; must be slug-safe after normalisation.
    in_path: bool = False
    # Suggest values from what's already in the catalog (free text stays allowed).
    suggest: bool = True
    help: str = ""


FIELDS: List[Field] = [
    Field("customer", "Customer", required=True, in_path=True,
          help="Customer name, use 'NR' for internal recordings"),
    Field("project", "Project", required=True, in_path=True),
    Field("robot", "Robot", required=True, in_path=True),
    Field("date", "Recording date", type="date", required=True, in_path=True, suggest=False,
          help="YYYY-MM-DD, prefilled from the bag start time when available"),
    Field("site", "Site", required=True, in_path=True),
    Field("robot_task", "Robot task", in_path=True,
          help="e.g. mapping, row_following, weeding"),
    Field("sensors", "Sensors", type="list",
          help="sensor_ids from the sensor registry"),
    Field("has_gps", "Has GPS", type="bool", suggest=False,
          help="Auto-detected from NavSatFix topics"),
    Field("stack_version", "Stack version",
          help="<repo>@<tag or commit>, e.g. horti_nav_stack@v2.3.6"),
    Field("ai_model_metadata", "AI model metadata", type="json", suggest=False,
          help='JSON, e.g. {"name": "seg_v3", "version": "1.2"}'),
    Field("length_s", "Length [s]", type="float", suggest=False,
          help="Auto-detected from the bag"),
    Field("responsible", "Responsible person"),
    Field("comment", "Comment", suggest=False),
]

FIELD_BY_NAME: Dict[str, Field] = {f.name: f for f in FIELDS}
SCALAR_FIELDS = [f for f in FIELDS if f.type != "list"]

# Fields filled by the platform itself, not by the uploader.
SYSTEM_FIELDS = ["dataset_id", "path", "size_bytes", "file_count", "storage_format",
                 "topics", "marina_ref", "status", "created_at", "updated_at"]


class ValidationError(ValueError):
    def __init__(self, errors: Dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _coerce(f: Field, value: Any) -> Any:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if f.type == "text":
        return str(value).strip()
    if f.type == "date":
        if isinstance(value, (_dt.date, _dt.datetime)):
            return value.strftime("%Y-%m-%d")
        return _dt.datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    if f.type == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        raise ValueError("expected true/false")
    if f.type == "float":
        return float(value)
    if f.type == "list":
        if isinstance(value, str):
            value = [v for v in re.split(r"[,;\s]+", value) if v]
        return sorted({str(v).strip() for v in value if str(v).strip()})
    if f.type == "json":
        import json
        return json.loads(value) if isinstance(value, str) else value
    raise ValueError(f"unknown field type {f.type}")


def normalize(meta: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
    """Coerce and validate user-supplied metadata.

    partial=True is used for bulk edits: only the given keys are checked and
    required fields may be absent (but not blanked).
    """
    out: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for f in FIELDS:
        if partial and f.name not in meta:
            continue
        try:
            v = _coerce(f, meta.get(f.name))
        except (ValueError, TypeError) as e:
            errors[f.name] = str(e)
            continue
        if f.required and v is None:
            errors[f.name] = "required"
            continue
        if f.in_path and v is not None and not slug(str(v)):
            errors[f.name] = "must contain letters or digits"
            continue
        out[f.name] = v
    unknown = set(meta) - set(FIELD_BY_NAME) - set(SYSTEM_FIELDS)
    for k in unknown:
        errors[k] = "unknown field"
    if errors:
        raise ValidationError(errors)
    return out


def slug(value: str) -> str:
    """Folder-safe lowercase identifier: 'Field Test #3' -> 'field-test-3'."""
    s = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return s.strip("-")


def schema_json() -> List[Dict[str, Any]]:
    return [f.__dict__.copy() for f in FIELDS]

