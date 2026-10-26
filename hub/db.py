"""SQLite catalog: datasets, the sensor registry, and the dataset<->sensor link."""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .schema import FIELD_BY_NAME, SCALAR_FIELDS

_SQL_TYPES = {"text": "TEXT", "date": "TEXT", "bool": "INTEGER", "float": "REAL", "json": "TEXT"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class Catalog:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        cols = ",\n".join(f"  {f.name} {_SQL_TYPES[f.type]}" for f in SCALAR_FIELDS)
        self.conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS datasets (
          dataset_id TEXT PRIMARY KEY,
          path TEXT UNIQUE NOT NULL,
          source_path TEXT,
          size_bytes INTEGER,
          file_count INTEGER,
          storage_format TEXT,
          topics TEXT,
          marina_ref TEXT,
          status TEXT NOT NULL DEFAULT 'ok',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
        {cols}
        );
        CREATE TABLE IF NOT EXISTS sensors (
          sensor_id TEXT PRIMARY KEY,
          sensor_type TEXT NOT NULL,
          model TEXT,
          driver_version TEXT,
          comment TEXT
        );
        CREATE TABLE IF NOT EXISTS dataset_sensors (
          dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
          sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
          PRIMARY KEY (dataset_id, sensor_id)
        );
        """)
        # Add columns for schema fields introduced after the DB was created.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(datasets)")}
        for f in SCALAR_FIELDS:
            if f.name not in existing:
                self.conn.execute(f"ALTER TABLE datasets ADD COLUMN {f.name} {_SQL_TYPES[f.type]}")
        self.conn.commit()

    # ---------------------------------------------------------------- sensors
    def upsert_sensor(self, sensor: Dict[str, Any]) -> None:
        if not sensor.get("sensor_id") or not sensor.get("sensor_type"):
            raise ValueError("sensor_id and sensor_type are required")
        self.conn.execute(
            """INSERT INTO sensors (sensor_id, sensor_type, model, driver_version, comment)
               VALUES (:sensor_id, :sensor_type, :model, :driver_version, :comment)
               ON CONFLICT(sensor_id) DO UPDATE SET sensor_type=excluded.sensor_type,
                 model=excluded.model, driver_version=excluded.driver_version,
                 comment=excluded.comment""",
            {k: sensor.get(k) for k in ("sensor_id", "sensor_type", "model",
                                        "driver_version", "comment")},
        )
        self.conn.commit()

    def sensors(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sensors ORDER BY sensor_id")]

    def _check_sensors(self, ids: Iterable[str]) -> None:
        known = {s["sensor_id"] for s in self.sensors()}
        missing = sorted(set(ids) - known)
        if missing:
            raise ValueError(f"unknown sensor_id(s): {', '.join(missing)} — register them first")

    # --------------------------------------------------------------- datasets
    def insert(self, meta: Dict[str, Any], system: Dict[str, Any]) -> str:
        sensors = meta.get("sensors") or []
        self._check_sensors(sensors)
        row = {f.name: _to_sql(f.name, meta.get(f.name)) for f in SCALAR_FIELDS}
        row.update({
            "dataset_id": system["dataset_id"],
            "path": system["path"],
            "source_path": system.get("source_path"),
            "size_bytes": system.get("size_bytes"),
            "file_count": system.get("file_count"),
            "storage_format": system.get("storage_format"),
            "topics": json.dumps(system.get("topics") or []),
            "marina_ref": system.get("marina_ref"),
            "status": system.get("status", "ok"),
            "created_at": _now(),
            "updated_at": _now(),
        })
        keys = ", ".join(row)
        self.conn.execute(
            f"INSERT INTO datasets ({keys}) VALUES ({', '.join(':' + k for k in row)})", row)
        self._set_sensors(row["dataset_id"], sensors)
        self.conn.commit()
        return row["dataset_id"]

    def _set_sensors(self, dataset_id: str, sensors: Iterable[str]) -> None:
        self.conn.execute("DELETE FROM dataset_sensors WHERE dataset_id = ?", (dataset_id,))
        self.conn.executemany(
            "INSERT INTO dataset_sensors (dataset_id, sensor_id) VALUES (?, ?)",
            [(dataset_id, s) for s in sensors])

    def update(self, dataset_id: str, meta: Dict[str, Any], system: Optional[Dict[str, Any]] = None,
               commit: bool = True) -> None:
        sets = {k: _to_sql(k, v) for k, v in meta.items() if k in FIELD_BY_NAME and k != "sensors"}
        sets.update(system or {})
        sets["updated_at"] = _now()
        self.conn.execute(
            f"UPDATE datasets SET {', '.join(f'{k} = :{k}' for k in sets)} WHERE dataset_id = :_id",
            {**sets, "_id": dataset_id})
        if "sensors" in meta:
            self._check_sensors(meta["sensors"] or [])
            self._set_sensors(dataset_id, meta["sensors"] or [])
        if commit:
            self.conn.commit()

    def delete(self, dataset_id: str) -> None:
        self.conn.execute("DELETE FROM datasets WHERE dataset_id = ?", (dataset_id,))
        self.conn.commit()

    def get(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query({"dataset_id": dataset_id})
        return rows[0] if rows else None

    def find_by_source(self, source_path: str) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT dataset_id FROM datasets WHERE source_path = ?", (source_path,))]

    def query(self, filters: Optional[Dict[str, Any]] = None, text: str = "",
              limit: int = 1000) -> List[Dict[str, Any]]:
        """Filter by any metadata field.

        filters values: a scalar (equality), a list (IN), or for date/float
        fields a dict {"min": .., "max": ..}. "sensors" matches datasets that
        contain *all* listed sensors; "sensor_type" (scalar or list) matches datasets
        with a sensor of any of those types.
        """
        where, params = [], {}
        for i, (k, v) in enumerate((filters or {}).items()):
            if v in (None, "", []):
                continue
            p = f"p{i}"
            if k == "sensors":
                ids = v if isinstance(v, list) else [v]
                for j, sid in enumerate(ids):
                    where.append(f"d.dataset_id IN (SELECT dataset_id FROM dataset_sensors "
                                 f"WHERE sensor_id = :{p}_{j})")
                    params[f"{p}_{j}"] = sid
            elif k == "sensor_type":
                types = v if isinstance(v, list) else [v]
                names = [f":{p}_{j}" for j in range(len(types))]
                where.append(f"d.dataset_id IN (SELECT ds.dataset_id FROM dataset_sensors ds "
                             f"JOIN sensors s USING (sensor_id) "
                             f"WHERE s.sensor_type IN ({', '.join(names)}))")
                params.update({f"{p}_{j}": t for j, t in enumerate(types)})
            elif k in FIELD_BY_NAME or k in ("dataset_id", "status", "storage_format"):
                if isinstance(v, dict):
                    if v.get("min") not in (None, ""):
                        where.append(f"d.{k} >= :{p}_min")
                        params[f"{p}_min"] = _to_sql(k, v["min"])
                    if v.get("max") not in (None, ""):
                        where.append(f"d.{k} <= :{p}_max")
                        params[f"{p}_max"] = _to_sql(k, v["max"])
                elif isinstance(v, list):
                    names = [f":{p}_{j}" for j in range(len(v))]
                    where.append(f"d.{k} IN ({', '.join(names)})")
                    params.update({f"{p}_{j}": _to_sql(k, x) for j, x in enumerate(v)})
                else:
                    where.append(f"d.{k} = :{p}")
                    params[p] = _to_sql(k, v)
            else:
                raise ValueError(f"cannot filter on {k}")
        if text:
            where.append("(d.path LIKE :text OR d.comment LIKE :text OR d.topics LIKE :text)")
            params["text"] = f"%{text}%"
        sql = (
            "SELECT d.*, (SELECT group_concat(sensor_id) FROM dataset_sensors ds "
            "WHERE ds.dataset_id = d.dataset_id) AS sensor_ids FROM datasets d"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY d.date DESC, d.created_at DESC LIMIT :limit"
        )
        params["limit"] = limit
        return [_from_row(r) for r in self.conn.execute(sql, params)]

    def distinct_values(self) -> Dict[str, List[Any]]:
        """Existing values per field, used for filter dropdowns and suggestions."""
        out: Dict[str, List[Any]] = {}
        for f in SCALAR_FIELDS:
            if f.suggest:
                out[f.name] = [r[0] for r in self.conn.execute(
                    f"SELECT DISTINCT {f.name} FROM datasets WHERE {f.name} IS NOT NULL "
                    f"ORDER BY {f.name}")]
        out["sensors"] = [s["sensor_id"] for s in self.sensors()]
        out["sensor_type"] = sorted({s["sensor_type"] for s in self.sensors()})
        return out


def _to_sql(name: str, value: Any) -> Any:
    f = FIELD_BY_NAME.get(name)
    if value is None or f is None:
        return value
    if f.type == "bool":
        if isinstance(value, str):
            return 1 if value.lower() in ("1", "true", "yes") else 0
        return int(bool(value))
    if f.type == "json":
        return json.dumps(value)
    return value


def _from_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for f in SCALAR_FIELDS:
        if d.get(f.name) is None:
            continue
        if f.type == "bool":
            d[f.name] = bool(d[f.name])
        elif f.type == "json":
            d[f.name] = json.loads(d[f.name])
    d["topics"] = json.loads(d["topics"]) if d.get("topics") else []
    ids = d.pop("sensor_ids", None)
    d["sensors"] = sorted(ids.split(",")) if ids else []
    return d
