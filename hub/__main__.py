"""CLI: python -m hub <command> --help"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from .db import Catalog
from .ingest import bulk_update, ingest, marina_pending, marina_sync, reindex
from .legacy import import_csv, scan
from .marina_backend import Marina
from .schema import FIELDS, ValidationError


def _kv(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"expected key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hub", description="Dataset hub")
    ap.add_argument("-c", "--config", help="config JSON (default: $DATASET_HUB_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="start the web UI")

    p = sub.add_parser("ingest", help="upload one dataset folder")
    p.add_argument("source")
    p.add_argument("meta", nargs="*", help="field=value, fields: " + ", ".join(f.name for f in FIELDS))
    p.add_argument("--mode", choices=["copy", "move", "register"])

    p = sub.add_parser("scan-legacy", help="scan old NAS tree into a review CSV")
    p.add_argument("root")
    p.add_argument("-o", "--out", default="legacy_scan.csv")
    p.add_argument("--default", nargs="*", metavar="field=value",
                   help="prefill a column for every row (e.g. customer=NR)")

    p = sub.add_parser("import-csv", help="ingest reviewed CSV rows marked import=yes")
    p.add_argument("csv")
    p.add_argument("--mode", choices=["copy", "move", "register"])
    p.add_argument("--dry-run", action="store_true", help="only validate the rows")

    p = sub.add_parser("bulk-set", help="set metadata for every dataset matching a filter")
    p.add_argument("--where", nargs="*", default=[], metavar="field=value")
    p.add_argument("--set", nargs="+", required=True, metavar="field=value", dest="changes")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")

    p = sub.add_parser("list", help="query the catalog")
    p.add_argument("--where", nargs="*", default=[], metavar="field=value")
    p.add_argument("-q", "--text", default="")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sensor-add", help="register / update a sensor")
    p.add_argument("sensor_id")
    p.add_argument("sensor_type")
    p.add_argument("--model", default="")
    p.add_argument("--driver-version", default="")
    p.add_argument("--comment", default="")

    p = sub.add_parser("marina-sync",
                       help="push datasets that are not (or not correctly) in the marina registry")
    p.add_argument("--where", nargs="*", default=[], metavar="field=value")
    p.add_argument("--force", action="store_true", help="re-push even if already up to date")
    p.add_argument("--dry-run", action="store_true", help="only list what would be pushed")

    sub.add_parser("sensors", help="list registered sensors")
    sub.add_parser("reindex", help="rebuild catalog from dataset.json sidecars")

    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)

    if args.cmd == "serve":
        from .server import serve
        serve(cfg)
        return 0

    cat = Catalog(cfg.db)
    marina = Marina(cfg.marina_bin, cfg.marina_registry) if cfg.marina_registry else None
    try:
        if args.cmd == "ingest":
            rec = ingest(cfg, cat, Path(args.source), _kv(args.meta), mode=args.mode, marina=marina)
            print(f"{rec['dataset_id']}  ->  {rec['path']}")
        elif args.cmd == "scan-legacy":
            n = scan(Path(args.root), Path(args.out), _kv(args.default))
            print(f"{n} datasets written to {args.out} — review it, set import=yes, "
                  f"then run import-csv")
        elif args.cmd == "import-csv":
            res = import_csv(cfg, cat, Path(args.csv), mode=args.mode, marina=marina,
                             dry_run=args.dry_run)
            for r in res["ok"]:
                print(f"ok    line {r['line']}: {r['source_path']} -> {r.get('path', '(dry run)')}")
            for r in res["errors"]:
                print(f"ERROR line {r['line']}: {r['source_path']}: {r['error']}")
            return 1 if res["errors"] else 0
        elif args.cmd == "bulk-set":
            hits = cat.query(_kv(args.where))
            print(f"{len(hits)} datasets match")
            if hits and not args.yes and input("apply? [y/N] ").lower() != "y":
                return 1
            recs = bulk_update(cfg, cat, [h["dataset_id"] for h in hits], _kv(args.changes),
                               marina=marina)
            print(f"updated {len(recs)}")
        elif args.cmd == "list":
            rows = cat.query(_kv(args.where), text=args.text)
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
            for r in rows if not args.json else []:
                print(f"{r['dataset_id']}  {r['date'] or '':10}  {r['customer']}/{r['project']}"
                      f"/{r['robot']}  {r['site']}  {r['path']}")
        elif args.cmd == "marina-sync":
            if marina is None:
                raise ValueError("marina_registry is not set in the config")
            hits = cat.query(_kv(args.where))
            todo = hits if args.force else marina_pending(hits)
            print(f"{len(todo)} of {len(hits)} datasets to push to {cfg.marina_registry}")
            failed = 0
            for r in todo:
                if args.dry_run:
                    print(f"  {r['dataset_id']}  {r.get('marina_ref') or '-'}")
                    continue
                rec = marina_sync(cat, marina, r["dataset_id"], force=args.force)
                ok = rec["status"] != "marina_push_failed"
                failed += not ok
                print(f"{'ok    ' if ok else 'FAILED'} {rec['dataset_id']}  {rec.get('marina_ref') or ''}")
            return 1 if failed else 0
        elif args.cmd == "sensor-add":
            cat.upsert_sensor({"sensor_id": args.sensor_id, "sensor_type": args.sensor_type,
                               "model": args.model, "driver_version": args.driver_version,
                               "comment": args.comment})
        elif args.cmd == "sensors":
            for s in cat.sensors():
                print(f"{s['sensor_id']:20} {s['sensor_type']:12} {s['model'] or '':20} "
                      f"{s['driver_version'] or ''}")
        elif args.cmd == "reindex":
            print(f"added {reindex(cfg, cat)} datasets")
    except ValidationError as e:
        for k, v in e.errors.items():
            print(f"  {k}: {v}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
