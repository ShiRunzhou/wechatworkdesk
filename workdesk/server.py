from __future__ import annotations

import json
import hashlib
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import __version__
from .store import DeskError

WEB = Path(__file__).parent / "web"
ID = r"([a-f0-9]{32})"


def make_server(store, port=8770):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            # The route is sufficient for diagnostics; no messages, bodies or credentials.
            pass

        def allowed_host(self):
            return self.headers.get("Host", "").lower() in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def send(self, status, content, mime="application/json; charset=utf-8", filename=None):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "只允许从本机地址访问"})
            path = urlsplit(self.path).path
            try:
                if path == "/api/health":
                    return self.send(200, {"status": "ok", "version": __version__,
                                           "instance": hashlib.sha256(str(store.root).casefold().encode()).hexdigest()})
                if path == "/api/state":
                    return self.send(200, store.dashboard())
                match = re.fullmatch(f"/api/sources/{ID}/download", path)
                if match:
                    source, file = store.source(match[1])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(file.stat().st_size))
                    self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(file.name)}")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with file.open("rb") as source_stream:
                        while block := source_stream.read(1024 * 1024):
                            self.wfile.write(block)
                    return
                match = re.fullmatch(f"/api/handoffs/{ID}/download", path)
                if match:
                    with store.connect() as con:
                        item = store.one(con, "handoffs", match[1])
                    return self.send(200, item["markdown"].encode("utf-8"), "text/markdown; charset=utf-8", f"交接包-{match[1][:8]}.md")
                match = re.fullmatch(f"/api/runs/{ID}", path)
                if match:
                    context = store.context(match[1])
                    # Context contains frozen result_json after publication; useful for original review.
                    return self.send(200, context)
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path in files:
                    name, mime = files[path]
                    return self.send(200, (WEB / name).read_bytes(), mime)
                return self.send(404, {"error": "未找到页面"})
            except DeskError as error:
                return self.send(400, {"error": str(error)})
            except OSError:
                return self.send(500, {"error": "本地文件暂时无法读取"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if (not self.allowed_host() or (origin and origin not in allowed)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.close_connection = True
                return self.send(403, {"error": "操作须从本机工作台发起"})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.close_connection = True
                return self.send(415, {"error": "操作须使用 JSON"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 1024 * 1024:
                    self.close_connection = True
                    return self.send(413, {"error": "操作内容为空或过大"})
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise DeskError("操作格式错误")
                path = urlsplit(self.path).path
                match = re.fullmatch(f"/api/matters/{ID}/feedback", path)
                if match:
                    return self.send(200, store.feedback("matter", match[1], payload))
                match = re.fullmatch(f"/api/backgrounds/{ID}/feedback", path)
                if match:
                    return self.send(200, store.feedback("background", match[1], payload))
                match = re.fullmatch(f"/api/matters/{ID}/handoff", path)
                if match:
                    return self.send(200, store.handoff(match[1], payload.get("version"), payload.get("note", "")))
                return self.send(404, {"error": "未找到操作"})
            except (DeskError, ValueError, UnicodeError) as error:
                return self.send(400, {"error": str(error)})
            except OSError:
                return self.send(500, {"error": "保存失败，请保留输入后重试"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(store, port=8770):
    server = make_server(store, port)
    print(f"工作事项台 http://127.0.0.1:{server.server_port}/ （Ctrl+C 关闭）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
