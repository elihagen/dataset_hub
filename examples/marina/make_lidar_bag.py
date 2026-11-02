#!/usr/bin/env python3
"""Write a rosbag2 folder with a realistic lidar recording (CDR PointCloud2 in MCAP).

The mock bags from scripts/make_mock_datasets.py are a few kB of JSON, which is
too small to show what marina's compression does. This one has real
sensor_msgs/msg/PointCloud2 messages (float32 x/y/z/intensity, sensor noise),
which is what marina compresses on push.

    python examples/marina/make_lidar_bag.py ./demo_lidar_run --scans 30
"""
import argparse
import datetime as dt
import math
import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from make_mock_datasets import MCAP_MAGIC, _rec, _str  # noqa: E402

TOPIC = "/sensors/ouster/points"
POINTCLOUD2_DEF = """std_msgs/Header header
uint32 height
uint32 width
PointField[] fields
bool is_bigendian
uint32 point_step
uint32 row_step
uint8[] data
bool is_dense
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
================================================================================
MSG: sensor_msgs/PointField
uint8 INT8=1
uint8 UINT8=2
uint8 INT16=3
uint8 UINT16=4
uint8 INT32=5
uint8 UINT32=6
uint8 FLOAT32=7
uint8 FLOAT64=8
string name
uint32 offset
uint8 datatype
uint32 count
"""


class Cdr:
    """Little-endian CDR writer; alignment is relative to the end of the 4-byte header."""

    def __init__(self):
        self.buf = bytearray(b"\x00\x01\x00\x00")

    def _align(self, n):
        self.buf += b"\x00" * (-(len(self.buf) - 4) % n)

    def u8(self, v):
        self.buf += struct.pack("<B", v)

    def u32(self, v):
        self._align(4)
        self.buf += struct.pack("<I", v)

    def i32(self, v):
        self._align(4)
        self.buf += struct.pack("<i", v)

    def string(self, s):
        b = s.encode() + b"\x00"
        self.u32(len(b))
        self.buf += b

    def blob(self, b):
        self.u32(len(b))
        self.buf += b


def scan(t: float, rings: int, cols: int, rng: random.Random) -> bytes:
    """One revolution of a spinning lidar in a vineyard-ish scene: ground + rows of plants."""
    pts = bytearray()
    for r in range(rings):
        elev = math.radians(-22.5 + 45.0 * r / (rings - 1))
        for c in range(cols):
            az = 2 * math.pi * c / cols + 0.05 * t
            # Ground for downward beams, a plant row every 2.5 m sideways, sky (no return) above.
            if elev < 0:
                rng_m = min(1.2 / -math.sin(elev), 60.0)
            else:
                side = abs(math.sin(az)) or 1e-6
                rng_m = 2.5 / side if 2.5 / side < 40 else 0.0
            if rng_m:
                rng_m += rng.gauss(0, 0.015)  # ~1.5 cm range noise
            x = rng_m * math.cos(elev) * math.cos(az)
            y = rng_m * math.cos(elev) * math.sin(az)
            z = rng_m * math.sin(elev)
            intensity = 0.0 if not rng_m else 30 + 200 * rng.random()
            pts += struct.pack("<ffff", x, y, z, intensity)
    return bytes(pts)


def pointcloud2(sec: int, nsec: int, rings: int, cols: int, data: bytes) -> bytes:
    w = Cdr()
    w.i32(sec)
    w.u32(nsec)
    w.string("os_lidar")
    w.u32(rings)
    w.u32(cols)
    w.u32(4)
    for i, name in enumerate(("x", "y", "z", "intensity")):
        w.string(name)
        w.u32(4 * i)
        w.u8(7)  # FLOAT32
        w.u32(1)
    w.u8(0)  # is_bigendian
    w.u32(16)
    w.u32(16 * cols)
    w.blob(data)
    w.u8(1)  # is_dense
    return bytes(w.buf)


def make(out: Path, scans: int = 30, rings: int = 64, cols: int = 512, hz: float = 10.0,
         start: str = "2026-06-12T09:00:00") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    start_dt = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)
    start_ns = int(start_dt.timestamp() * 1e9)
    rng = random.Random(42)

    schema = POINTCLOUD2_DEF.encode()
    defs = _rec(0x03, struct.pack("<H", 1) + _str("sensor_msgs/msg/PointCloud2") + _str("ros2msg")
                + struct.pack("<I", len(schema)) + schema)
    defs += _rec(0x04, struct.pack("<HH", 1, 1) + _str(TOPIC) + _str("cdr") + struct.pack("<I", 0))
    data = MCAP_MAGIC + _rec(0x01, _str("ros2") + _str("make_lidar_bag")) + defs
    # Chunked + indexed, like rosbag2's MCAP writer. marina reads messages through
    # the chunk index; an unchunked file is pushed without its messages.
    chunk_indexes = b""
    for k in range(scans):
        ns = start_ns + int(k / hz * 1e9)
        body = pointcloud2(ns // 10**9, ns % 10**9, rings, cols, scan(k / hz, rings, cols, rng))
        records = _rec(0x05, struct.pack("<HIQQ", 1, k, ns, ns) + body)
        chunk_start = len(data)
        data += _rec(0x06, struct.pack("<QQQI", ns, ns, len(records), 0) + _str("")
                     + struct.pack("<Q", len(records)) + records)
        index_start = len(data)
        data += _rec(0x07, struct.pack("<HI", 1, 16) + struct.pack("<QQ", ns, 0))
        offsets = struct.pack("<HQ", 1, index_start)
        chunk_indexes += _rec(0x08, struct.pack("<QQQQ", ns, ns, chunk_start, index_start - chunk_start)
                              + struct.pack("<I", len(offsets)) + offsets
                              + struct.pack("<Q", len(data) - index_start) + _str("")
                              + struct.pack("<QQ", len(records), len(records)))
    end_ns = start_ns + int((scans - 1) / hz * 1e9)

    data += _rec(0x0F, struct.pack("<I", 0))
    summary_start = len(data)
    count_map = struct.pack("<HQ", 1, scans)
    stats = struct.pack("<QHIIIIQQ", scans, 1, 1, 0, 0, scans, start_ns, end_ns)
    data += defs + chunk_indexes + _rec(0x0B, stats + struct.pack("<I", len(count_map)) + count_map)
    data += _rec(0x02, struct.pack("<QQI", summary_start, 0, 0)) + MCAP_MAGIC

    name = out.name
    (out / f"{name}_0.mcap").write_bytes(data)
    (out / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n"
        "  version: 5\n"
        "  storage_identifier: mcap\n"
        "  duration:\n"
        f"    nanoseconds: {end_ns - start_ns}\n"
        "  starting_time:\n"
        f"    nanoseconds_since_epoch: {start_ns}\n"
        f"  message_count: {scans}\n"
        "  topics_with_message_count:\n"
        "    - topic_metadata:\n"
        f"        name: {TOPIC}\n"
        "        type: sensor_msgs/msg/PointCloud2\n"
        "        serialization_format: cdr\n"
        "        offered_qos_profiles: \"\"\n"
        f"      message_count: {scans}\n"
        "  compression_format: \"\"\n"
        "  compression_mode: \"\"\n"
        "  relative_file_paths:\n"
        f"    - {name}_0.mcap\n")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", default="./demo_lidar_run")
    ap.add_argument("--scans", type=int, default=30, help="number of PointCloud2 messages")
    ap.add_argument("--rings", type=int, default=64)
    ap.add_argument("--cols", type=int, default=512)
    a = ap.parse_args()
    p = make(Path(a.out), a.scans, a.rings, a.cols)
    print(f"{p}  ({sum(f.stat().st_size for f in p.iterdir()) / 1e6:.1f} MB)")
