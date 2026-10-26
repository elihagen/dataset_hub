"""End-to-end test on mock data:  python -m unittest discover -s tests"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hub import bag_info  # noqa: E402
from hub.config import Config  # noqa: E402
from hub.db import Catalog  # noqa: E402
from hub.ingest import SIDECAR, bulk_update, ingest, marina_pending, marina_sync, reindex  # noqa: E402
from hub.legacy import import_csv, scan  # noqa: E402
from hub.marina_backend import Marina  # noqa: E402
from hub.schema import ValidationError  # noqa: E402
import make_mock_datasets  # noqa: E402


class FakeMarina(Marina):
    """Records registry contents instead of calling the marina binary."""
    def __init__(self):
        super().__init__(registry="test")
        self.remote, self.fail = {}, False

    def push(self, ref, path):
        if self.fail:
            raise RuntimeError("registry unreachable")
        self.remote[ref] = json.loads((Path(path) / SIDECAR).read_text())
        return ""

    def remove(self, ref):
        self.remote.pop(ref, None)
        return ""


META = {"customer": "Customer A", "project": "Vineyard", "robot": "Mower 1",
        "site": "Osnabrueck", "robot_task": "mapping", "responsible": "elisa"}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.nas = t / "old_nas"
        self.bags = make_mock_datasets.make(self.nas)
        self.cfg = Config(storage_root=str(t / "storage"), db_path=str(t / "catalog.sqlite"))
        self.cat = Catalog(self.cfg.db)
        for sid, typ in [("ouster_os1", "lidar"), ("ublox_f9p", "gps"), ("xsens_mti", "imu")]:
            self.cat.upsert_sensor({"sensor_id": sid, "sensor_type": typ, "driver_version": "1.0"})

    def tearDown(self):
        self.tmp.cleanup()

    def vineyard_run(self):
        return self.nas / "customer_a/vineyard/2026-06-12/run1"

    def test_inspect_detects_bag_facts(self):
        info = bag_info.inspect(self.vineyard_run())
        self.assertEqual(info["bag_count"], 1)
        self.assertEqual(info["date"], "2026-06-12")
        self.assertEqual(info["length_s"], 300)
        self.assertTrue(info["has_gps"])
        self.assertEqual(info["storage_format"], "mcap")
        # No NavSatFix topic -> no GPS.
        no_gps = bag_info.inspect(self.nas / "NR/field_tests/rosbag2_2026_05_04-13_31_38")
        self.assertFalse(no_gps["has_gps"])

    def test_ingest_enforces_structure_and_keeps_original(self):
        rec = ingest(self.cfg, self.cat, self.vineyard_run(),
                     {**META, "sensors": "ouster_os1, ublox_f9p"})
        rel = Path(rec["path"]).relative_to(self.cfg.storage)
        self.assertEqual(rel.parts[:3], ("customer-a", "vineyard", "mower-1"))
        self.assertTrue(rel.parts[3].startswith("2026-06-12_osnabrueck_mapping_"))
        self.assertTrue((Path(rec["path"]) / SIDECAR).exists())
        self.assertTrue(self.vineyard_run().exists(), "copy mode must keep the original")
        self.assertEqual(rec["sensors"], ["ouster_os1", "ublox_f9p"])
        self.assertTrue(rec["has_gps"])
        self.assertEqual(rec["length_s"], 300)

    def test_ingest_rejects_missing_and_unknown(self):
        with self.assertRaises(ValidationError) as e:
            ingest(self.cfg, self.cat, self.vineyard_run(), {"customer": "NR"})
        self.assertIn("project", e.exception.errors)
        with self.assertRaises(ValueError):
            ingest(self.cfg, self.cat, self.vineyard_run(), {**META, "sensors": "nope"})
        self.assertEqual(self.cat.query(), [])
        self.assertFalse(any(self.cfg.storage.rglob("*.mcap")), "failed ingest must not leave files")

    def test_filters(self):
        ingest(self.cfg, self.cat, self.vineyard_run(), {**META, "sensors": "ouster_os1"})
        ingest(self.cfg, self.cat, self.nas / "NR/field_tests/rosbag2_2026_05_04-13_31_38",
               {**META, "customer": "NR", "project": "internal", "sensors": "ublox_f9p"})
        self.assertEqual(len(self.cat.query({"customer": "NR"})), 1)
        self.assertEqual(len(self.cat.query({"has_gps": True})), 1)
        self.assertEqual(len(self.cat.query({"sensor_type": "lidar"})), 1)
        self.assertEqual(len(self.cat.query({"date": {"max": "2026-05-31"}})), 1)
        self.assertEqual(len(self.cat.query({"length_s": {"min": 200}})), 1)
        self.assertEqual(len(self.cat.query(text="NavSatFix")), 1)
        self.assertEqual(len(self.cat.query({"customer": ["NR", "Customer A"]})), 2)
        vals = self.cat.distinct_values()
        self.assertEqual(vals["customer"], ["Customer A", "NR"])

    def test_bulk_update_relocates(self):
        a = ingest(self.cfg, self.cat, self.vineyard_run(), dict(META))
        b = ingest(self.cfg, self.cat, self.nas / "customer_a/vineyard/2026-06-12/run2", dict(META))
        recs = bulk_update(self.cfg, self.cat, [a["dataset_id"], b["dataset_id"]],
                           {"project": "Vineyard 2026", "stack_version": "2.3.0"})
        for r in recs:
            self.assertIn("/vineyard-2026/", r["path"])
            self.assertTrue(Path(r["path"]).exists())
            self.assertEqual(json.loads((Path(r["path"]) / SIDECAR).read_text())["stack_version"],
                             "2.3.0")
        self.assertFalse((self.cfg.storage / "customer-a/vineyard").exists(), "empty dirs pruned")
        with self.assertRaises(ValidationError):
            bulk_update(self.cfg, self.cat, [a["dataset_id"]], {"customer": ""})

    def test_legacy_scan_and_import(self):
        out = Path(self.tmp.name) / "scan.csv"
        n = scan(self.nas, out, {"customer": "NR"})
        self.assertEqual(n, 5)
        with out.open() as f:
            rows = list(csv.DictReader(f))
        # Reviewer fills the missing columns for a subset and marks it for import.
        for r in rows:
            if "field_tests" in r["source_path"]:
                r.update(project="field tests", robot="tractor", site="nr-yard", import_flag="yes")
                r["import"] = r.pop("import_flag")
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        res = import_csv(self.cfg, self.cat, out)
        self.assertEqual(len(res["ok"]), 2, res)
        self.assertEqual(res["errors"], [])
        again = import_csv(self.cfg, self.cat, out)
        self.assertEqual(len(again["errors"]), 2, "re-import is refused")

    def test_reindex_from_sidecars(self):
        ingest(self.cfg, self.cat, self.vineyard_run(), {**META, "sensors": "ouster_os1"})
        fresh = Catalog(Path(self.tmp.name) / "rebuilt.sqlite")
        for s in self.cat.sensors():
            fresh.upsert_sensor(s)
        self.assertEqual(reindex(self.cfg, fresh), 1)
        self.assertEqual(fresh.query()[0]["sensors"], ["ouster_os1"])

    def test_marina_push_retry_and_rename(self):
        m = FakeMarina()
        m.fail = True
        rec = ingest(self.cfg, self.cat, self.vineyard_run(), dict(META), marina=m)
        self.assertEqual(rec["status"], "marina_push_failed")
        self.assertIsNone(rec["marina_ref"])
        self.assertEqual(len(marina_pending(self.cat.query())), 1)

        m.fail = False
        rec = marina_sync(self.cat, m, rec["dataset_id"])
        self.assertEqual(rec["status"], "ok")
        ref = rec["marina_ref"]
        self.assertTrue(ref.startswith("customer-a/vineyard-mower-1-2026-06-12:"))
        self.assertEqual(m.remote[ref]["marina_ref"], ref, "pushed copy carries its sidecar")
        self.assertEqual(marina_pending(self.cat.query()), [])

        # Metadata in the ref changes -> pushed under the new ref, old one removed.
        rec, = bulk_update(self.cfg, self.cat, [rec["dataset_id"]], {"site": "Bramsche"}, marina=m)
        self.assertIn(":bramsche:", rec["marina_ref"])
        self.assertEqual(list(m.remote), [rec["marina_ref"]])
        # Changes outside the ref don't trigger a push.
        m.fail = True
        rec, = bulk_update(self.cfg, self.cat, [rec["dataset_id"]], {"comment": "x"}, marina=m)
        self.assertEqual(rec["status"], "ok")

    def test_viewer_file_endpoint(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        from hub.server import App, make_handler

        rec = ingest(self.cfg, self.cat, self.vineyard_run(), dict(META))
        app = App(self.cfg)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_port}"

        def get(path, **headers):
            try:
                with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers)) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), b""

        files = json.loads(get(f"/api/datasets/{rec['dataset_id']}/files")[2])["files"]
        self.assertEqual(files, ["run1_0.mcap"])
        url = f"/files/{rec['dataset_id']}/run1_0.mcap"
        whole = (Path(rec["path"]) / "run1_0.mcap").read_bytes()

        status, h, body = get(url, Range="bytes=0-7", Origin="https://app.foxglove.dev")
        self.assertEqual((status, body), (206, b"\x89MCAP0\r\n"))
        self.assertEqual(h["Content-Range"], f"bytes 0-7/{len(whole)}")
        self.assertEqual(h["Access-Control-Allow-Origin"], "https://app.foxglove.dev")
        self.assertEqual(get(url, Range="bytes=-8")[2], whole[-8:])
        self.assertEqual(get(url)[2], whole)
        self.assertEqual(get(url, Range=f"bytes={len(whole)}-")[0], 416)
        # Other websites get no CORS grant; only bag files inside the dataset are served.
        self.assertNotIn("Access-Control-Allow-Origin", get(url, Origin="https://evil.example")[1])
        self.assertEqual(get(f"/files/{rec['dataset_id']}/dataset.json")[0], 404)
        self.assertEqual(get(f"/files/{rec['dataset_id']}/..%2F..%2F..%2Fcatalog.sqlite")[0], 404)

    @unittest.skipUnless(shutil.which(os.environ.get("MARINA_BIN", "marina")),
                         "marina CLI not installed (set MARINA_BIN to test against it)")
    def test_real_marina_keeps_dataset(self):
        home = Path(self.tmp.name) / "home"
        env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share")}
        with mock.patch.dict(os.environ, env):  # isolated marina config, cache and registry
            m = Marina(os.environ.get("MARINA_BIN", "marina"), "test")
            m._run(["registry", "add", "test", f"folder://{Path(self.tmp.name) / 'registry'}"])
            rec = ingest(self.cfg, self.cat, self.vineyard_run(), dict(META), marina=m)
            self.assertEqual(rec["status"], "ok")
            self.assertTrue(any(Path(rec["path"]).glob("*.mcap")), "push must not move the data away")
            listing = m._run(["list", "--remote", "--registry", "test", "--format", "json"])
            self.assertIn(rec["dataset_id"], listing)


if __name__ == "__main__":
    unittest.main()
