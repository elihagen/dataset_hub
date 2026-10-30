"""Workstation web UI + JSON API (stdlib only, no install needed)."""
from __future__ import annotations

import json
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config
from .db import Catalog
from .github import StackVersions
from .ingest import bulk_update, ingest, marina_sync, suggest_metadata, target_path
from .marina_backend import Marina
from .schema import ValidationError, normalize, schema_json

STATIC = Path(__file__).resolve().parent.parent / "static"
# What Foxglove/Lichtblick can open as a remote file (not rosbag2 .db3).
PREVIEW_SUFFIXES = (".mcap", ".bag")
CHUNK = 1 << 20


def preview_files(dataset_path: str) -> List[str]:
    root = Path(dataset_path)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.suffix in PREVIEW_SUFFIXES and p.is_file())


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.catalog = Catalog(cfg.db)
        self.lock = threading.Lock()
        self.marina = Marina(cfg.marina_bin, cfg.marina_registry) if cfg.marina_registry else None
        self.stack_versions = StackVersions(cfg.github_org, cfg.stack_repos) if cfg.stack_repos else None
        v = urlparse(cfg.viewer_url)
        self.viewer_origin = f"{v.scheme}://{v.netloc}" if v.netloc else None

    def browse_allowed(self, path: Path) -> bool:
        path = path.resolve()
        for root in self.cfg.browse_roots:
            try:
                path.relative_to(Path(root).expanduser().resolve())
                return True
            except ValueError:
                continue
        return False


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter console
            pass

        # ------------------------------------------------------------ helpers
        def _send(self, status: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                status, body = self.route(method, url.path, qs)
                self._send(status, body)
            except ValidationError as e:
                self._send(400, {"error": "validation", "fields": e.errors})
            except (ValueError, KeyError, OSError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path.startswith("/files/"):
                return self._serve_file(head=False)
            self._dispatch("GET")

        def do_HEAD(self):
            if self.path.startswith("/files/"):
                return self._serve_file(head=True)
            self.send_error(405)

        def do_OPTIONS(self):  # CORS preflight from the viewer
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
            self.send_header("Access-Control-Allow-Headers", "Range")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        # ---------------------------------------------------- viewer file access
        def _cors(self) -> None:
            # Only the configured viewer may read files cross-origin; there is no
            # auth, so "*" would let any website read the datasets.
            if app.viewer_origin and self.headers.get("Origin") == app.viewer_origin:
                self.send_header("Access-Control-Allow-Origin", app.viewer_origin)
                self.send_header("Access-Control-Expose-Headers",
                                 "Accept-Ranges, Content-Length, Content-Range")
                self.send_header("Vary", "Origin")

        def _resolve_file(self) -> Optional[Path]:
            parts = urlparse(self.path).path.split("/", 3)  # "", "files", id, rel
            if len(parts) != 4:
                return None
            with app.lock:
                rec = app.catalog.get(unquote(parts[2]))
            if rec is None:
                return None
            root = Path(rec["path"]).resolve()
            target = (root / unquote(parts[3])).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                return None
            return target if target.suffix in PREVIEW_SUFFIXES and target.is_file() else None

        def _serve_file(self, head: bool) -> None:
            target = self._resolve_file()
            if target is None:
                self.send_response(404)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            size = target.stat().st_size
            start, end, status = 0, size - 1, 200
            # Single byte range only; anything else gets the whole file (allowed by RFC 9110).
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
            if m and m.groups() != ("", ""):
                a, b = m.groups()
                if a == "":
                    start = max(size - int(b), 0)
                else:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self._cors()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            try:
                with target.open("rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the viewer cancels range requests it no longer needs

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ------------------------------------------------------------- routes
        def route(self, method: str, path: str, qs: Dict[str, str]):
            cat = app.catalog
            if method == "GET" and path == "/api/schema":
                with app.lock:
                    return 200, {"fields": schema_json(), "values": cat.distinct_values(),
                                 "sensors": cat.sensors(), "path_template": app.cfg.path_template,
                                 "storage_root": str(app.cfg.storage),
                                 "browse_roots": list(app.cfg.browse_roots),
                                 "marina": app.cfg.marina_registry if app.marina else None,
                                 "viewer_url": app.cfg.viewer_url,
                                 "github_org": app.cfg.github_org}
            if method == "GET" and path == "/api/stack_versions":
                if app.stack_versions is None:
                    return 200, {"versions": [], "errors": {}}
                return 200, app.stack_versions.fetch()
            if method == "GET" and path == "/api/datasets":
                filters = json.loads(qs.get("filters") or "{}")
                with app.lock:
                    return 200, cat.query(filters, text=qs.get("q", ""))
            if method == "GET" and path.startswith("/api/datasets/") and path.endswith("/files"):
                with app.lock:
                    rec = cat.get(path.split("/")[3])
                if rec is None:
                    return 404, {"error": "not found"}
                return 200, {"files": preview_files(rec["path"])}
            if method == "GET" and path.startswith("/api/datasets/"):
                with app.lock:
                    rec = cat.get(path.rsplit("/", 1)[1])
                return (200, rec) if rec else (404, {"error": "not found"})
            if method == "GET" and path == "/api/browse":
                p = Path(qs.get("path") or app.cfg.browse_roots[0]).expanduser()
                if not app.browse_allowed(p):
                    return 403, {"error": f"outside browse_roots: {p}"}
                dirs = sorted(c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
                return 200, {"path": str(p.resolve()), "parent": str(p.resolve().parent),
                             "dirs": dirs}
            if method == "POST" and path == "/api/inspect":
                src = Path(self._body()["source"]).expanduser()
                if not app.browse_allowed(src):
                    return 403, {"error": f"outside browse_roots: {src}"}
                return 200, suggest_metadata(src)
            if method == "POST" and path == "/api/preview_path":
                body = self._body()
                try:
                    meta = normalize(body.get("meta", {}))
                except ValidationError as e:
                    return 200, {"path": None, "missing": e.errors}
                return 200, {"path": str(target_path(app.cfg, meta, "<id>"))}
            if method == "POST" and path == "/api/datasets":
                body = self._body()
                src = Path(body["source"]).expanduser()
                if not app.browse_allowed(src):
                    return 403, {"error": f"outside browse_roots: {src}"}
                with app.lock:
                    rec = ingest(app.cfg, cat, src, body.get("meta", {}),
                                 mode=body.get("mode"), marina=app.marina)
                return 201, rec
            if method == "POST" and path == "/api/bulk":
                body = self._body()
                with app.lock:
                    recs = bulk_update(app.cfg, cat, body["ids"], body["changes"],
                                       marina=app.marina)
                return 200, {"updated": len(recs),
                             "marina_failed": sum(r["status"] == "marina_push_failed" for r in recs)}
            if method == "POST" and path == "/api/marina/sync":
                if app.marina is None:
                    return 400, {"error": "marina_registry is not set in the config"}
                body = self._body()
                with app.lock:
                    recs = [marina_sync(cat, app.marina, did, force=bool(body.get("force")))
                            for did in body["ids"]]
                failed = [r["dataset_id"] for r in recs if r["status"] == "marina_push_failed"]
                return 200, {"pushed": len(recs) - len(failed), "failed": failed}
            if method == "POST" and path == "/api/sensors":
                with app.lock:
                    cat.upsert_sensor(self._body())
                return 200, {"ok": True}
            return 404, {"error": f"no route {method} {path}"}

    return Handler


def serve(cfg: Config) -> None:
    app = App(cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(app))
    print(f"dataset hub on http://{cfg.host}:{cfg.port}  (storage: {cfg.storage})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
