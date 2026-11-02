"""Extract what can be derived automatically from a dataset folder.

Works without ROS: rosbag2 `metadata.yaml` is parsed with PyYAML when
available, otherwise with a small regex fallback that only needs the fields
used here. Plain MCAP files without metadata.yaml are read with the `mcap`
package if it is installed.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import checks

GPS_TYPES = {"sensor_msgs/msg/NavSatFix", "sensor_msgs/NavSatFix", "gps_msgs/msg/GPSFix"}
BAG_SUFFIXES = {".mcap": "mcap", ".db3": "sqlite3", ".bag": "ros1"}


def folder_stats(path: Path) -> Dict[str, int]:
    size = count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                size += (Path(root) / name).stat().st_size
                count += 1
            except OSError:
                pass
    return {"size_bytes": size, "file_count": count}


def _parse_rosbag2_yaml(text: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            info = yaml.safe_load(text)["rosbag2_bagfile_information"]
            topics = [
                {"name": t["topic_metadata"]["name"], "type": t["topic_metadata"]["type"],
                 "count": t.get("message_count", 0)}
                for t in info.get("topics_with_message_count") or []
            ]
        except (yaml.YAMLError, KeyError, TypeError) as e:
            return {"topics": [], "parse_error": str(e).splitlines()[0][:200]}
        return {
            "storage_format": info.get("storage_identifier"),
            "duration_ns": (info.get("duration") or {}).get("nanoseconds"),
            "start_ns": (info.get("starting_time") or {}).get("nanoseconds_since_epoch"),
            "message_count": info.get("message_count"),
            "files": info.get("relative_file_paths") or [],
            "topics": topics,
        }

    def grab(pattern: str) -> Optional[str]:
        m = re.search(pattern, text)
        return m.group(1) if m else None

    if "rosbag2_bagfile_information" not in text:
        return {"topics": [], "parse_error": "not a rosbag2 metadata.yaml"}
    topics = []
    for block in _topic_blocks(text):
        name = re.search(r"^\s*name:\s*(\S+)", block, re.M)
        typ = re.search(r"^\s*type:\s*(\S+)", block, re.M)
        count = re.search(r"^\s*(?:-\s*)?message_count:\s*(\d+)", block, re.M)
        if name and typ:
            topics.append({"name": name.group(1), "type": typ.group(1),
                           "count": int(count.group(1)) if count else 0})
    files = re.search(r"relative_file_paths:\s*\n((?:\s*-\s*\S+\s*\n)+)", text)
    dur = grab(r"\n  duration:\s*\n\s*nanoseconds:\s*(\d+)")
    start = grab(r"\n  starting_time:\s*\n\s*nanoseconds_since_epoch:\s*(\d+)")
    msgs = grab(r"\n  message_count:\s*(\d+)")
    return {
        "storage_format": grab(r"storage_identifier:\s*(\S+)"),
        "duration_ns": int(dur) if dur else None,
        "start_ns": int(start) if start else None,
        "message_count": int(msgs) if msgs else None,
        "files": re.findall(r"-\s*(\S+)", files.group(1)) if files else [],
        "topics": topics,
    }


def _topic_blocks(text: str) -> List[str]:
    """Split `topics_with_message_count` into one text block per list entry.

    Splits on the list's own indentation: `rosbags`-converted bags put a QoS
    list between name and type, and message_count before or after the topic.
    """
    if "topics_with_message_count:" not in text:
        return []
    blocks: List[List[str]] = []
    indent = None
    for line in text.split("topics_with_message_count:", 1)[1].splitlines()[1:]:
        if not line.strip():
            continue
        lead = len(line) - len(line.lstrip())
        if indent is None:
            indent = lead
        if lead < indent or (lead == indent and not line.lstrip().startswith("-")):
            break
        if lead == indent:
            blocks.append([])
        blocks[-1].append(line)
    return ["\n".join(b) for b in blocks]


def _read_mcap(path: Path) -> Optional[Dict[str, Any]]:
    try:
        from mcap.reader import make_reader  # type: ignore
    except ImportError:
        return None
    try:
        with open(path, "rb") as f:
            summary = make_reader(f).get_summary()
    except Exception:
        return None
    if summary is None or summary.statistics is None:
        return None
    st = summary.statistics
    counts = st.channel_message_counts or {}
    topics = []
    for cid, ch in summary.channels.items():
        schema = summary.schemas.get(ch.schema_id)
        topics.append({"name": ch.topic, "type": schema.name if schema else "",
                       "count": counts.get(cid, 0)})
    return {
        "storage_format": "mcap",
        "duration_ns": st.message_end_time - st.message_start_time,
        "start_ns": st.message_start_time,
        "message_count": st.message_count,
        "topics": topics,
    }


def find_bags(path: Path) -> List[Path]:
    """rosbag2 directories (with metadata.yaml) or loose bag files below path."""
    bags: List[Path] = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "metadata.yaml" in files:
            bags.append(Path(root))
            dirs[:] = []
            continue
        bags.extend(Path(root) / f for f in files if Path(f).suffix in BAG_SUFFIXES)
    return sorted(bags)


def inspect(path: Path) -> Dict[str, Any]:
    """Return derived metadata for a dataset folder (may contain several bags)."""
    path = Path(path)
    out: Dict[str, Any] = folder_stats(path)
    bags = find_bags(path)
    infos = []
    out["warnings"] = []
    for bag in bags:
        info = None
        if bag.is_dir():
            info = _parse_rosbag2_yaml((bag / "metadata.yaml").read_text(errors="replace"))
        elif bag.suffix == ".mcap":
            info = _read_mcap(bag)
        if info is None:
            info = {"storage_format": BAG_SUFFIXES.get(bag.suffix), "topics": []}
        infos.append(info)
        # Prefix with the bag's folder/file when the dataset has several bags.
        where = f"{bag.relative_to(path)}: " if bag != path else ""
        out["warnings"] += [{**w, "message": where + w["message"]}
                            for w in checks.check_bag(bag, info)]
    if not bags:
        out["warnings"].append({"level": "error",
                                "message": "no rosbag2/mcap/db3/bag found in this folder"})

    out["bag_count"] = len(bags)
    formats = sorted({i["storage_format"] for i in infos if i.get("storage_format")})
    out["storage_format"] = ",".join(formats) or None
    durations = [i["duration_ns"] for i in infos if i.get("duration_ns")]
    out["length_s"] = round(sum(durations) / 1e9, 3) if durations else None
    starts = [i["start_ns"] for i in infos if i.get("start_ns")]
    out["date"] = (
        _dt.datetime.fromtimestamp(min(starts) / 1e9, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        if starts else None
    )
    topics: Dict[str, Dict[str, Any]] = {}
    for i in infos:
        for t in i.get("topics", []):
            agg = topics.setdefault(t["name"], {"name": t["name"], "type": t["type"], "count": 0})
            agg["count"] += t.get("count") or 0
    out["topics"] = sorted(topics.values(), key=lambda t: t["name"])
    out["has_gps"] = any(t["type"] in GPS_TYPES and t["count"] > 0 for t in topics.values()) \
        if topics else None
    return out
