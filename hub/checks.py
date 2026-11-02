"""Sanity checks on recorded data, shown as warnings before upload.

Cheap by design (headers, footers, metadata only) so they also run over SMB
during a legacy scan. Each issue is {"level": "error"|"warning"|"info",
"message"}: "error" means the data is broken or unusable, "warning" that it
looks wrong, "info" that it is worth knowing but usually fine.
Nothing here blocks an upload; the uploader decides.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List

MCAP_MAGIC = b"\x89MCAP0\r\n"
SQLITE_MAGIC = b"SQLite format 3\x00"
# Recordings before this are almost certainly a robot without a set clock.
EARLIEST_PLAUSIBLE = _dt.datetime(2015, 1, 1, tzinfo=_dt.timezone.utc)
MAX_LISTED = 8
# Only published on events, so empty in most recordings: never worth a warning.
EVENT_TYPES = {
    "lifecycle_msgs/msg/TransitionEvent",
    "rcl_interfaces/msg/ParameterEvent",
    "rcl_interfaces/msg/Log",
    "rosbag2_interfaces/msg/WriteSplitEvent",
}


def _is_sensor(topic: Dict[str, Any]) -> bool:
    """Sensor streams should never be empty; command/teleop topics often are."""
    return topic["name"].startswith("/sensors/") or (
        topic["type"].startswith("sensor_msgs/") and topic["type"] != "sensor_msgs/msg/Joy")


def _issue(level: str, message: str) -> Dict[str, str]:
    return {"level": level, "message": message}


def _names(items: List[str]) -> str:
    more = f" (+{len(items) - MAX_LISTED} more)" if len(items) > MAX_LISTED else ""
    return ", ".join(items[:MAX_LISTED]) + more


def check_file(path: Path) -> List[Dict[str, str]]:
    """Header/footer checks for a single bag file."""
    try:
        size = path.stat().st_size
    except OSError as e:
        return [_issue("error", f"{path.name}: not readable ({e.strerror})")]
    if size == 0:
        return [_issue("error", f"{path.name}: file is empty (0 bytes)")]
    issues = []
    try:
        with path.open("rb") as f:
            head = f.read(len(SQLITE_MAGIC))
            if path.suffix == ".mcap":
                f.seek(max(0, size - len(MCAP_MAGIC)))
                tail = f.read(len(MCAP_MAGIC))
    except OSError as e:
        return [_issue("error", f"{path.name}: not readable ({e.strerror})")]
    if path.suffix == ".mcap":
        if not head.startswith(MCAP_MAGIC):
            issues.append(_issue("error", f"{path.name}: not a valid MCAP file (bad header)"))
        elif tail != MCAP_MAGIC:
            issues.append(_issue("error", f"{path.name}: MCAP file is truncated or was not closed "
                                          "properly (recording crashed?); try `mcap recover`"))
    elif path.suffix == ".db3":
        if head != SQLITE_MAGIC:
            issues.append(_issue("error", f"{path.name}: not a valid SQLite/db3 file"))
        leftovers = [s for s in ("-wal", "-shm") if path.with_name(path.name + s).exists()]
        if leftovers:
            issues.append(_issue("warning", f"{path.name}: {'/'.join(leftovers)} file next to it, "
                                            "recording was probably not stopped cleanly"))
    return issues


def check_bag(bag: Path, info: Dict[str, Any]) -> List[Dict[str, str]]:
    """Checks for one rosbag2 folder or loose bag file, given its parsed info."""
    issues: List[Dict[str, str]] = []
    if bag.is_dir():
        if info.get("parse_error"):
            return [_issue("error", f"metadata.yaml could not be read: {info['parse_error']}")]
        listed = info.get("files") or []
        missing = [f for f in listed if not (bag / f).exists()]
        if missing:
            issues.append(_issue("error", f"bag file(s) listed in metadata.yaml are missing: "
                                          f"{_names(missing)}"))
        files = [bag / f for f in listed if f not in missing]
        if not listed:
            files = sorted(p for p in bag.iterdir() if p.suffix in (".mcap", ".db3"))
            if not files:
                issues.append(_issue("error", "metadata.yaml but no .mcap/.db3 file in the folder"))
    else:
        files = [bag]
    for f in files:
        issues.extend(check_file(f))

    topics = info.get("topics") or []
    if not topics:
        if bag.suffix != ".bag":
            issues.append(_issue("warning", "no topics could be read from the bag"))
        return issues
    total = info.get("message_count")
    if total is None:
        total = sum(t.get("count") or 0 for t in topics)
    if total == 0:
        issues.append(_issue("error", "bag contains no messages"))
        return issues
    empty = [t for t in topics if not t.get("count") and t["type"] not in EVENT_TYPES]
    sensors = [t["name"] for t in empty if _is_sensor(t)]
    others = [t["name"] for t in empty if not _is_sensor(t)]
    if sensors:
        issues.append(_issue("warning", f"{len(sensors)} sensor topic(s) recorded no data: "
                                        f"{_names(sensors)}"))
    if others:
        issues.append(_issue("info", f"{len(others)} other topic(s) have no messages (normal for "
                                     f"unused command/teleop topics): {_names(others)}"))
    if not info.get("duration_ns"):
        issues.append(_issue("warning", "recording duration is 0 or unknown"))
    start = info.get("start_ns")
    if start:
        t0 = _dt.datetime.fromtimestamp(start / 1e9, tz=_dt.timezone.utc)
        if t0 < EARLIEST_PLAUSIBLE or t0 > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1):
            issues.append(_issue("warning", f"start time {t0:%Y-%m-%d %H:%M} looks wrong "
                                            "(robot clock not set?), check the recording date"))
    return issues
