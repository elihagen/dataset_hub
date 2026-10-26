#!/usr/bin/env python3
"""Create a fake, messy "old NAS" tree with small rosbag2 folders.

The .mcap files are small but valid MCAP with JSON-encoded mock messages, so
marina can push them and Foxglove/Lichtblick can preview them; everything the
hub reads comes from metadata.yaml, exactly like on a real recording.

    python scripts/make_mock_datasets.py ./mock_nas
"""
import argparse
import datetime as dt
import json
import math
import struct
from pathlib import Path

TOPICS = {
    "lidar": ("/sensors/ouster/points", "sensor_msgs/msg/PointCloud2", 10),
    "camera": ("/sensors/cam_front/image_raw/compressed", "sensor_msgs/msg/CompressedImage", 15),
    "imu": ("/sensors/imu/data", "sensor_msgs/msg/Imu", 100),
    "gps": ("/sensors/gps/fix", "sensor_msgs/msg/NavSatFix", 5),
    "odom": ("/odom", "nav_msgs/msg/Odometry", 50),
}

# (legacy folder, start time, duration [s], sensors)
LAYOUT = [
    ("NR/field_tests/rosbag2_2026_05_04-13_31_38", "2026-05-04T13:31:38", 120, ["lidar", "imu", "odom"]),
    ("NR/field_tests/rosbag2_2026_09_24-10_18_16", "2026-09-24T10:18:16", 8, ["gps", "imu"]),
    ("customer_a/vineyard/2026-06-12/run1", "2026-06-12T09:00:00", 300, ["lidar", "camera", "gps", "imu"]),
    ("customer_a/vineyard/2026-06-12/run2", "2026-06-12T11:30:00", 240, ["lidar", "camera", "gps", "imu"]),
    ("old_stuff/test bag (copy)", "2025-11-03T15:00:00", 60, ["camera", "odom"]),
]


def metadata_yaml(name: str, start: dt.datetime, duration_s: float, sensors) -> str:
    topics, total = [], 0
    for s in sensors:
        topic, typ, hz = TOPICS[s]
        n = int(hz * duration_s)
        total += n
        topics.append(
            f"    - topic_metadata:\n"
            f"        name: {topic}\n"
            f"        type: {typ}\n"
            f"        serialization_format: cdr\n"
            f"        offered_qos_profiles: \"\"\n"
            f"      message_count: {n}\n")
    start_ns = int(start.replace(tzinfo=dt.timezone.utc).timestamp() * 1e9)
    return (
        "rosbag2_bagfile_information:\n"
        "  version: 5\n"
        "  storage_identifier: mcap\n"
        "  duration:\n"
        f"    nanoseconds: {int(duration_s * 1e9)}\n"
        "  starting_time:\n"
        f"    nanoseconds_since_epoch: {start_ns}\n"
        f"  message_count: {total}\n"
        "  topics_with_message_count:\n" + "".join(topics) +
        "  compression_format: \"\"\n"
        "  compression_mode: \"\"\n"
        "  relative_file_paths:\n"
        f"    - {name}_0.mcap\n"
    )


MCAP_MAGIC = b"\x89MCAP0\r\n"


def _rec(op: int, body: bytes) -> bytes:
    return struct.pack("<BQ", op, len(body)) + body


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<I", len(b)) + b


PREVIEW_HZ = 2  # mock message rate, independent of the counts in metadata.yaml

# JSON-encoded mock messages that Foxglove/Lichtblick decode without ROS message
# definitions: gps -> Map panel, imu/odom -> Plot. Lidar and camera get channels only.
# sensor: (schema name, JSON schema properties, message(t_s, sec, nsec))
MOCK_MESSAGES = {
    "gps": ("foxglove.LocationFix", ["timestamp", "frame_id", "latitude", "longitude", "altitude"],
            lambda t, sec, nsec: {"timestamp": {"sec": sec, "nsec": nsec}, "frame_id": "gps",
                                  "latitude": 52.2799 + 0.0005 * math.sin(t / 30),
                                  "longitude": 8.0472 + 0.0008 * math.cos(t / 30), "altitude": 63.0}),
    "imu": ("mock.Imu", ["linear_acceleration", "angular_velocity"],
            lambda t, sec, nsec: {"linear_acceleration": {"x": 0.3 * math.sin(t), "y": 0.1 * math.cos(t / 2), "z": 9.81},
                                  "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.2 * math.sin(t / 30)}}),
    "odom": ("mock.Odometry", ["x", "y", "yaw", "speed"],
             lambda t, sec, nsec: {"x": 40 * math.sin(t / 30), "y": 40 * math.cos(t / 30),
                                   "yaw": (t / 30) % (2 * math.pi), "speed": 1.3}),
}


def mcap_bytes(sensors, start_ns: int, duration_s: float) -> bytes:
    """Small valid MCAP: header, one schema/channel per topic, mock messages, summary, footer."""
    defs, msgs, counts, n_total = b"", b"", {}, 0
    for i, s in enumerate(sensors, start=1):
        topic, typ, _ = TOPICS[s]
        name, props, _ = MOCK_MESSAGES.get(s, (typ, [], None))
        schema = json.dumps({"type": "object", "properties": {p: {} for p in props}}).encode()
        defs += _rec(0x03, struct.pack("<H", i) + _str(name) + _str("jsonschema")
                     + struct.pack("<I", len(schema)) + schema)
        defs += _rec(0x04, struct.pack("<HH", i, i) + _str(topic) + _str("json")
                     + struct.pack("<I", 0))  # empty metadata map
    for k in range(int(duration_s * PREVIEW_HZ) + 1):
        t = k / PREVIEW_HZ
        ns = start_ns + int(t * 1e9)
        for i, s in enumerate(sensors, start=1):
            if s not in MOCK_MESSAGES:
                continue
            body = json.dumps(MOCK_MESSAGES[s][2](t, ns // 10**9, ns % 10**9)).encode()
            msgs += _rec(0x05, struct.pack("<HIQQ", i, k, ns, ns) + body)
            counts[i] = counts.get(i, 0) + 1
            n_total += 1
    data = MCAP_MAGIC + _rec(0x01, _str("ros2") + _str("make_mock_datasets"))
    data += defs + msgs + _rec(0x0F, struct.pack("<I", 0))  # data end, crc 0 = not checked
    summary_start = len(data)
    count_map = b"".join(struct.pack("<HQ", c, n) for c, n in counts.items())
    end_ns = start_ns + int(duration_s * 1e9) if n_total else 0
    stats = struct.pack("<QHIIIIQQ", n_total, len(sensors), len(sensors), 0, 0, 0,
                        start_ns if n_total else 0, end_ns)
    data += defs + _rec(0x0B, stats + struct.pack("<I", len(count_map)) + count_map)
    return data + _rec(0x02, struct.pack("<QQI", summary_start, 0, 0)) + MCAP_MAGIC


def make(root: Path) -> list:
    created = []
    for rel, start, dur, sensors in LAYOUT:
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        name = d.name.replace(" ", "_")
        start_dt = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)
        (d / "metadata.yaml").write_text(metadata_yaml(name, start_dt, dur, sensors))
        (d / f"{name}_0.mcap").write_bytes(
            mcap_bytes(sensors, int(start_dt.timestamp() * 1e9), dur))
        created.append(d)
    # A folder without any bag, which the scanner must ignore.
    (root / "old_stuff" / "notes").mkdir(parents=True, exist_ok=True)
    (root / "old_stuff" / "notes" / "readme.txt").write_text("not a dataset\n")
    return created


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="./mock_nas")
    args = ap.parse_args()
    for d in make(Path(args.root)):
        print(d)
