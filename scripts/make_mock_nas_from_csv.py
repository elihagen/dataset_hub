#!/usr/bin/env python3
"""Build a mock of the *new* NAS layout from the legacy metadata scan.

Input: datasets.csv / datasets_sensors.csv written by extract_metadata.py
from the old NAS. Output: a folder tree in the structure agreed on in the
Data Management Focus Sprint (15 Oct 2026), with small mock files instead of
the real data, plus a catalog the hub UI can filter and search.

    <out>/
      recordings/   hub storage_root: {customer}/{project}/{robot}/{date}_{site}_{robot_task}_{id}/
                      dataset.json, metadata.yaml (real topics/counts/duration), <name>_0.mcap (mock)
      derived/      {customer}/{project}/{derived_type}/...   cut patches, merged bags, labels, annotations
      scans/        {customer}/{scan_project}/...              terrestrial laser scans (.PROJ)
      external/     {dataset}/...                              open-source datasets
      assets/       {kind}/...                                 sim assets, media, satellite images
      unsorted/     <legacy path>                              not classified, needs a manual decision
      MAPPING.csv   legacy path -> new path for every legacy row

Nothing on the old NAS or in the existing catalog is touched.

    python scripts/make_mock_nas_from_csv.py ../datasets.csv ../datasets_sensors.csv -o ./mock_nas_v2
    python -m hub -c config.mock_nas_v2.json serve
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hub import bag_info, config as config_mod  # noqa: E402
from hub.db import Catalog  # noqa: E402
from hub.ingest import SIDECAR, reindex, target_path  # noqa: E402
from hub.schema import ValidationError, normalize, slug  # noqa: E402
from make_mock_datasets import mcap_bytes  # noqa: E402

csv.field_size_limit(sys.maxsize)

# ------------------------------------------------------------------ classification
# Legacy rows that are not recordings. First match wins; the target is formatted
# with the regex groups and the date/year of the row.
NON_RECORDING_RULES = [
    # saat processing pipeline (derived from saat raw recordings)
    (r"^saat/(?:(2024)/)?4_cut_patches(_1)?/(patch_\d+)", "derived/nr/saat/cut-patches{1}/{year}/{2}"),
    (r"^saat/(?:2024/)?1_merged_data/", "derived/nr/saat/merged/{year}"),
    (r"^saat/(?:2024/)?5_cut_patches_visualized", "derived/nr/saat/visualizations/{year}"),
    (r"^saat/data_analysis_steps/output-(\d{4})/(patch_\d+)", "derived/nr/saat/analysis/{0}/{1}"),
    (r"^saat/data_(?:analysis|cleanup)_steps", "derived/nr/saat/analysis/misc"),
    # labels / annotations / extracted images
    (r"^plantmap_labels/([^/]+)/([^/]+)", "derived/nr/plantmap/labels/{0}/{1}"),
    (r"^eic_kalkriese/simmetry[-_]ai[-_]annotations[-_](june|may)( 2)?/([^/]+)",
     "derived/nr/eic-kalkriese/annotations-simmetry-ai/{0}{1}/{2}"),
    (r"^eic_kalkriese/(\d{4}_\d\d_\d\d)/(classified_images|extracted_images|example_labels|simmetry_ai_annotations_may)",
     "derived/nr/eic-kalkriese/{1}/{0}"),
    (r"^kws/data_recording_([\d-]+)/(classified_images|extracted_images)", "derived/nr/kws/{1}/{0}"),
    (r"^misc/datasets/flex_rcnn_labelled", "derived/nr/flex-rcnn/labels"),
    (r"^misc/datasets/flex_rcnn", "derived/nr/flex-rcnn/images"),
    (r"^slam/slam_maps/([^/]+)", "derived/nr/slam/maps/{0}"),
    (r"^slam/slam_data/([^/]+)", "derived/nr/slam/processed/{0}"),
    (r"^misc/2025-05-02_robot_harddrive_backup/slam_(?:data|demo_station)", "derived/nr/slam/processed/harddrive-backup"),
    (r"^customers/haro/robot_envs_haro_([\d-]+)", "derived/haro/field-tests/robot-envs/{0}"),
    (r"^customers/agrosolar/(Plys(?:_correct)?)", "derived/agrosolar/agrosolar/pointclouds/{0}"),
    # terrestrial laser scan projects (raw, but no bags)
    (r"^customers/([^/]+)/(?:[^/]+/)?([^/]+)\.PROJ", "scans/{0}/{1}"),
    # open-source / public datasets
    (r"^eh_datasets/OpenSource/goose/([^/]+)", "external/goose/{0}"),
    (r"^eh_datasets/OpenSource/(nuscenes|dataset)", "external/{0}"),
    (r"^slam/public_data/([^/]+)", "external/{0}"),
    # assets
    (r"^eh_datasets/isaac/([^/]+)", "assets/sim/isaac/{0}"),
    (r"^isaac_sim_assets/([^/]+)", "assets/sim/isaac-sim-assets/{0}"),
    (r"^misc/Pleiades-Neo-Marseille-demo", "assets/satellite/pleiades-neo-marseille"),
    (r"^oof(?:/|$)", "assets/media/oof"),
]

# ------------------------------------------------------------------ metadata rules
PROJECT_RULES = [
    (r"^saat_raw_data/|^saat/|robot_harddrive_backup/saat/", "saat"),
    (r"robot_harddrive_backup/lalweco/", "lalweco"),
    (r"^slam/|robot_harddrive_backup/slam_data/", "slam"),
    (r"robot_harddrive_backup/2022_and_2023_unsorted_bags/", "unsorted-2022-2023"),
    (r"Baumschule_Bonheur", "baumschule-bonheur"),
    (r"wheat-flowers", "wheat-flowers"),
    (r"realsense_fps_experiments|^misc/20\d\d-\d\d-\d\d_", "sensor-experiments"),
    (r"Bad-Iburger-Weinberg", "bad-iburg-weinberg"),
    (r"^misc/datasets/flex_rcnn", "flex-rcnn"),
    (r"^Kalkriese_(rrf|zed)/|^eic_kalkriese/", "eic-kalkriese"),
    (r"^customers/(?:haro|firo)/", "field-tests"),
    (r"^customers/furo/friedrichshafen", "friedrichshafen"),
    (r"^customers/furo/HSOS_haste", "hsos-haste"),
    (r"^customers/agrosolar/", "agrosolar"),
]

SITE_RULES = [
    (r"kalkriese", "Kalkriese"),
    (r"friedrichshafen", "Friedrichshafen"),
    (r"marseille", "Marseille"),
    (r"arenshorst", "Arenshorst"),
    (r"bad.iburg", "Bad Iburg Weinberg"),
    (r"haste", "Haste"),
    (r"baumschule_bonheur", "Baumschule Bonheur"),
    (r"naturpark_harz", "Naturpark Harz"),
    (r"langsenkamp", "Langsenkamp"),
    (r"/cic/|_cic/|lero_cic", "CIC"),
    (r"^slam/(?:raw_data|slam_data)/([^/]+)", None),  # slam folders are named after the site
]

TASK_RULES = [
    (r"calibration", "calibration"),
    (r"k-turn", "k-turn"),
    (r"autonomous-driving", "autonomous-driving"),
    (r"^slam/|slam_data|sick_multiscan", "mapping"),
    (r"gps_experiments|fixposition", "gps-test"),
    (r"localization", "localization"),
    (r"realsense_fps", "sensor-test"),
    (r"aussaat-hacken", "sowing-hoeing"),
    (r"spritzen", "spraying"),
    (r"saat", "sowing"),
    (r"plantmap", "plant-mapping"),
    (r"headland|row-\d|oscillation|weird-map|robot-stop|hitch", "field-debugging"),
]

ROBOT_RE = re.compile(r"(?<![a-z])(lero0\d|babo)(?![a-z])", re.I)
SLAM_ROBOT_RE = re.compile(r"^slam/.*\d_(babo|furo)(?:_|/|$)", re.I)
DATE_RES = [
    re.compile(r"(20\d\d)[-_](\d\d)[-_](\d\d)"),
    re.compile(r"(?<!\d)(\d\d)_(\d\d)_(\d\d)[-_]"),  # haro: 24_02_19-...
]
# Lower = preferred copy when the same bag exists several times on the NAS.
SOURCE_PRIORITY = [(r"^saat_raw_data/", 0), (r"^saat/", 1), (r"robot_harddrive_backup/", 9)]
SENSOR_TYPE_PRIORITY = ["lidar", "camera", "gps", "laserscan", "imu", "magnetometer"]
PREVIEW_S = 20  # length of the mock .mcap, the real duration is in metadata.yaml


def _first(rules, text, default=None):
    for pattern, value in rules:
        m = re.search(pattern, text, re.I)
        if m:
            return value if value is not None else m.group(1).replace("_", " ").title()
    return default


def parse_date(row):
    d = row["date"]
    if d and "2015" <= d[:4] <= "2026":  # 2262-04-11 = broken ROS time in empty bags
        return d
    for part in reversed(row["rel_path"].split("/")):
        for rx in DATE_RES:
            m = rx.search(part)
            if not m:
                continue
            y, mo, da = m.groups()
            y = y if len(y) == 4 else "20" + y
            try:
                return dt.date(int(y), int(mo), int(da)).isoformat()
            except ValueError:
                continue
    return None


def is_recording(row):
    return row["kind"] in ("ros1", "ros2", "mcap") or row["file_extensions"].split(";")[0] in (".bag", ".db3", ".mcap")


def recording_meta(row, sensor_ids):
    p = row["rel_path"]
    robot = row["robot"] if row["robot"] not in ("", "lero") else None
    if robot is None:
        m = ROBOT_RE.search(p) or SLAM_ROBOT_RE.search(p)
        robot = m.group(1).lower() if m else ("lero" if row["robot"] == "lero" else "unknown")
    site = row["site"] or _first(SITE_RULES, p, "unknown")
    notes = []
    if row["error"]:
        notes.append(f"unreadable in legacy scan: {row['error'][:120]}")
    if float(row["size_gb"] or 0) == 0 or float(row["duration_s"] or 0) == 0:
        notes.append("empty or near-empty recording")
    if row["notes_files"]:
        notes.append(f"notes: {row['notes_files']}")
    return {
        "customer": row["customer"] or "NR",
        "project": _first(PROJECT_RULES, p, row["project"]),
        "robot": robot,
        "date": parse_date(row),
        "site": site,
        "robot_task": _first(TASK_RULES, p),
        "sensors": sorted(s for s in row["sensor_ids"].split(";") if s in sensor_ids),
        "comment": " | ".join([f"legacy: {p} ({row['format'] or row['kind']}, {row['size_gb']} GB)"] + notes),
    }


# ------------------------------------------------------------------ writers
def metadata_yaml(name, start_ns, duration_s, topics):
    total = sum(t["count"] for t in topics)
    body = "".join(
        f"    - topic_metadata:\n"
        f"        name: {t['name']}\n"
        f"        type: {t['type'] or 'unknown'}\n"
        f"        serialization_format: cdr\n"
        f"        offered_qos_profiles: \"\"\n"
        f"      message_count: {t['count']}\n" for t in topics)
    return (
        "rosbag2_bagfile_information:\n"
        "  version: 5\n"
        "  storage_identifier: mcap\n"
        "  duration:\n"
        f"    nanoseconds: {int(duration_s * 1e9)}\n"
        "  starting_time:\n"
        f"    nanoseconds_since_epoch: {start_ns}\n"
        f"  message_count: {total}\n"
        "  topics_with_message_count:\n" + body +
        "  compression_format: \"\"\n"
        "  compression_mode: \"\"\n"
        "  relative_file_paths:\n"
        f"    - {name}_0.mcap\n"
    )


def start_ns(row, date):
    try:
        t = dt.datetime.fromisoformat(row["start_time_utc"])
        if t.year == int(date[:4]):
            return int(t.timestamp() * 1e9)
    except ValueError:
        pass
    return int(dt.datetime.fromisoformat(date + "T12:00:00+00:00").timestamp() * 1e9)


def write_recording(dest, row, meta, dataset_id, topics):
    dest.mkdir(parents=True)
    name = Path(row["rel_path"]).name.replace(" ", "_") or dataset_id
    duration = float(row["duration_s"] or 0)
    t0 = start_ns(row, meta["date"])
    # topics with types/counts from the sensor scan, the rest by name only (still searchable)
    known = {t["name"] for t in topics}
    topics = topics + [{"name": n, "type": "", "count": 0}
                       for n in row["topics"].split(";") if n and n not in known]
    (dest / "metadata.yaml").write_text(metadata_yaml(name, t0, duration, topics))
    mock_sensors = [s for s in ("gps", "imu", "lidar", "camera") if s in row["sensor_types"].split(";")]
    (dest / f"{name}_0.mcap").write_bytes(mcap_bytes(mock_sensors, t0, min(duration, PREVIEW_S)))
    info = bag_info.inspect(dest)  # reindex takes these from the sidecar only
    meta = {**meta, "has_gps": info["has_gps"], "length_s": info["length_s"]}
    sidecar = {**meta, "dataset_id": dataset_id,
               "legacy": {k: row[k] for k in ("rel_path", "kind", "format", "size_gb", "file_count",
                                              "duration_s", "message_count", "error")}}
    (dest / SIDECAR).write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")


def write_manifest(dest, rows):
    dest.mkdir(parents=True, exist_ok=True)
    items = [{k: r[k] for k in ("dataset_id", "rel_path", "kind", "format", "date", "size_gb",
                                "file_count", "file_extensions")} for r in rows]
    (dest / "manifest.json").write_text(json.dumps({
        "mock": True, "items": len(items),
        "size_gb": round(sum(float(r["size_gb"] or 0) for r in rows), 3),
        "sources": items}, indent=2) + "\n")


# ------------------------------------------------------------------ main
def register_sensors(catalog, sensor_rows, dataset_rows):
    types = collections.defaultdict(collections.Counter)
    for s in sensor_rows:
        types[s["sensor_id"]][s["sensor_type"]] += 1
    for r in dataset_rows:  # ids that only appear in datasets.csv
        for sid in filter(None, r["sensor_ids"].split(";")):
            types[sid]
    for sid, c in sorted(types.items()):
        all_types = sorted(c, key=lambda t: SENSOR_TYPE_PRIORITY.index(t) if t in SENSOR_TYPE_PRIORITY else 99)
        catalog.upsert_sensor({"sensor_id": sid, "sensor_type": all_types[0] if all_types else "other",
                               "comment": "auto-registered from legacy scan"
                                          + (f", topics of type {', '.join(all_types)}" if len(all_types) > 1 else "")})
    return set(types)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("datasets_csv")
    ap.add_argument("sensors_csv")
    ap.add_argument("-o", "--out", default="./mock_nas_v2")
    ap.add_argument("-c", "--config", default="config.mock_nas_v2.json",
                    help="hub config whose storage_root is <out>/recordings (created if missing)")
    ap.add_argument("--force", action="store_true", help="delete <out> and the mock catalog first")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps({
            "_comment": "Mock of the new NAS layout, generated by scripts/make_mock_nas_from_csv.py",
            "storage_root": str(Path(out, "recordings").relative_to(cfg_path.parent)),
            "db_path": "./data/mock_nas_v2_catalog.sqlite",
            "path_template": "{customer}/{project}/{robot}/{date}_{site}_{robot_task}_{dataset_id}",
            "ingest_mode": "copy",
            "marina_registry": None,
            "browse_roots": ["~", "./mock_nas", str(out.relative_to(cfg_path.parent))],
            "host": "127.0.0.1",
            "port": 8081,
        }, indent=2) + "\n")
    cfg = config_mod.load(str(cfg_path))
    if cfg.storage != out / "recordings":
        raise SystemExit(f"storage_root in {cfg_path} must be {out / 'recordings'}, is {cfg.storage}")
    if out.exists() or cfg.db.exists():
        if not args.force:
            raise SystemExit(f"{out} or {cfg.db} exists, use --force to rebuild")
        shutil.rmtree(out, ignore_errors=True)
        cfg.db.unlink(missing_ok=True)

    rows = list(csv.DictReader(open(args.datasets_csv, newline="")))
    sensor_rows = list(csv.DictReader(open(args.sensors_csv, newline="")))
    topics_by_ds = collections.defaultdict(list)
    for s in sensor_rows:
        topics_by_ds[s["dataset_id"]].append(
            {"name": s["topic"], "type": s["msg_type"], "count": int(s["message_count"] or 0)})

    catalog = Catalog(cfg.db)
    sensor_ids = register_sensors(catalog, sensor_rows, rows)

    mapping, groups, seen = [], collections.defaultdict(list), {}
    rows.sort(key=lambda r: (_first(SOURCE_PRIORITY, r["rel_path"], 5), r["rel_path"]))
    for row in rows:
        p = row["rel_path"]
        entry = {"dataset_id": row["dataset_id"], "legacy_path": p, "category": "", "new_path": "",
                 "status": "ok", "note": ""}
        mapping.append(entry)
        rule = next(((rx, tpl) for rx, tpl in NON_RECORDING_RULES if re.search(rx, p)), None)
        if rule:
            m = re.search(rule[0], p)
            date = parse_date(row)
            target = rule[1].format(*(g or "" for g in m.groups()), year=(date or "unknown")[:4])
            entry.update(category=target.split("/")[0], new_path=target)
            groups[target].append(row)
            continue
        if not is_recording(row):
            target = "unsorted/" + p
            entry.update(category="unsorted", new_path=target, note="no rule, decide manually")
            groups[target].append(row)
            continue

        key = (Path(p).name, row["size_gb"], row["message_count"])
        if float(row["size_gb"] or 0) > 0 and key in seen:
            entry.update(category="recordings", status="duplicate", new_path=seen[key],
                         note="same folder name, size and message count as new_path")
            continue
        meta = recording_meta(row, sensor_ids)
        if meta["date"] is None:
            target = "unsorted/" + p
            entry.update(category="unsorted", new_path=target, note="recording without a date")
            groups[target].append(row)
            continue
        try:
            meta = normalize(meta)
        except ValidationError as e:
            entry.update(status="error", note=str(e))
            continue
        dest = target_path(cfg, meta, row["dataset_id"])
        write_recording(dest, row, meta, row["dataset_id"], topics_by_ds[row["dataset_id"]])
        entry.update(category="recordings", new_path=str(dest.relative_to(out)))
        seen[key] = entry["new_path"]

    for target, members in groups.items():
        write_manifest(out / target, members)

    with open(out / "MAPPING.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(mapping[0]))
        w.writeheader()
        w.writerows(sorted(mapping, key=lambda e: e["legacy_path"]))

    n = reindex(cfg, catalog)
    stats = collections.Counter((e["category"], e["status"]) for e in mapping)
    print(f"mock NAS: {out}")
    for (cat, status), c in sorted(stats.items()):
        print(f"  {cat or '-':11s} {status:10s} {c:6d}")
    print(f"catalog: {cfg.db}  ({n} recordings, {len(sensor_ids)} sensors)")
    print(f"start the UI:  python3 -m hub -c {cfg_path.name} serve   ->  http://{cfg.host}:{cfg.port}")


if __name__ == "__main__":
    main()
