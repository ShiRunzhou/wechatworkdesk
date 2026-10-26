from __future__ import annotations

import json
import hashlib
import re
import tempfile
import zipfile
import threading
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit, parse_qs

from . import __version__
from .store import DeskError

WEB = Path(__file__).parent / "web"
ID = r"([a-f0-9]{32})"


def make_server(store, port=8770, pipeline=None, website=None):
    from .flow import Pipeline
    pipeline = pipeline or Pipeline(store,recover=True)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            # The route is sufficient for diagnostics; no messages, bodies or credentials.
            pass

        def allowed_host(self):
            return self.headers.get("Host", "").lower() in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def authenticated(self):
            if website and not (website.authenticate_device(self.headers.get('Authorization'),urlsplit(self.path).path,self.command) or website.authenticate(self.headers.get('Authorization'))):
                self.close_connection = True
                self.send(401, {'error': '请登录 WeChatWorkDesk'}, headers={'WWW-Authenticate': 'Basic realm="WeChatWorkDesk", charset="UTF-8"'})
                return False
            return True

        def send(self, status, content, mime="application/json; charset=utf-8", filename=None, headers=None):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self' http://127.0.0.1:8771; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "只允许从本机地址访问"})
            if not self.authenticated():return
            path = urlsplit(self.path).path
            try:
                if path == "/api/health":
                    return self.send(200, {"status": "ok", "version": __version__,
                                           "pipeline": True,
                                           "instance": hashlib.sha256(str(store.root).casefold().encode()).hexdigest()})
                if path == '/api/pipeline/config':
                    return self.send(200,{**pipeline.settings.public(), 'website_mode': bool(website), 'analysis_scope':pipeline.analysis_scope() if hasattr(pipeline,'analysis_scope') else {}})
                if path=='/api/desktop/catalog':return self.send(200,{'objects':store.dashboard()['objects']})
                if path=='/api/device/state':return self.send(200,{'connected':False})
                if path == '/api/pipeline/sessions':
                    return self.send(200,pipeline.sessions('refresh=1' in urlsplit(self.path).query))
                if path == '/api/pipeline/jobs':
                    return self.send(200,{'jobs':pipeline.jobs()})
                match=re.fullmatch(f'/api/pipeline/jobs/{ID}(?:/(context|agent-request))?',path)
                if match:
                    if match[2]=='context':return self.send(200,pipeline.context(match[1]))
                    if match[2]=='agent-request':
                        pipeline.job(match[1]);p=pipeline.folder(match[1])/'agent-request.md'
                        if not p.exists():raise DeskError('Agent资料尚未准备好')
                        return self.send(200,{'text':p.read_text('utf8')})
                    return self.send(200,pipeline.public_job(match[1]))
                if path == "/api/cleanup":return self.send(200,store.cleanup_preview())
                if path == "/api/state":
                    return self.send(200, store.dashboard())
                if path=='/api/memory':return self.send(200,store.memory_state())
                if path=='/api/memory/download':
                    files=store.export_memory()['files'];buffer=io.BytesIO()
                    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
                        for file in files:archive.write(file,Path(file).name)
                    return self.send(200,buffer.getvalue(),'application/zip','工作记忆与历史档案.zip')
                if path=='/api/memory/search':
                    query=parse_qs(urlsplit(self.path).query)
                    return self.send(200,{'history':store.search_memory(query.get('q',[''])[0],query.get('object_id',[''])[0])})
                if path=='/api/storage':return self.send(200,store.storage_preview())
                match = re.fullmatch(f"/api/sources/{ID}/download", path)
                if match:
                    source, file = store.source(match[1])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(file.stat().st_size))
                    self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(Path(source['relative_path']).name)}")
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
                         '/memory.js':('memory.js','text/javascript; charset=utf-8'),
                         "/style.css": ("style.css", "text/css; charset=utf-8"),
                         "/shared.css": ("shared.css", "text/css; charset=utf-8"),
                         '/update':('update.html','text/html; charset=utf-8'),
                         '/update.js':('update.js','text/javascript; charset=utf-8'),
                         '/update.css':('update.css','text/css; charset=utf-8')}
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
            if website:allowed.add(website.origin)
            if (not self.allowed_host() or (origin and origin not in allowed)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.close_connection = True
                return self.send(403, {"error": "操作须从本机工作台发起"})
            if website and not origin:
                self.close_connection = True
                return self.send(403, {'error': '网站操作须声明同源地址'})
            if not self.authenticated():return
            if website and urlsplit(self.path).path == '/api/uploads':
                return self.upload()
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.close_connection = True
                return self.send(415, {"error": "操作须使用 JSON"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                limit=8*1024*1024 if urlsplit(self.path).path.startswith('/api/pipeline/') else 1024*1024
                if size < 1 or size > limit:
                    self.close_connection = True
                    return self.send(413, {"error": "操作内容为空或过大"})
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise DeskError("操作格式错误")
                path = urlsplit(self.path).path
                if path=='/api/memory':return self.send(200,store.save_memory(payload))
                if path=='/api/memory/scan':return self.send(200,store.scan_feedback())
                if path=='/api/storage/policy':return self.send(200,store.save_storage_policy(payload))
                if path=='/api/storage/clean':
                    with pipeline.guard:
                        if pipeline.running:raise DeskError('分析运行期间不能清理资料，请稍后再试')
                        return self.send(200,store.storage_clean(payload))
                if path=='/api/pipeline/device-paths':
                    if website:raise DeskError('网站无需配置电脑上的 WeFlow 路径')
                    from .device_paths import save_paths
                    return self.send(200,save_paths(pipeline.settings,payload))
                if path=='/api/cleanup':return self.send(200,store.cleanup_confirm(payload))
                if path=='/api/pipeline/config':
                    if website and payload.get('material_roots'):raise DeskError('网站背景资料请通过上传登记，不读取服务器其他目录')
                    return self.send(200,{**pipeline.settings.save(payload), 'website_mode': bool(website)})
                if website and path=='/api/objects':
                    return self.send(200,store.add_object(payload.get('name',''),payload.get('kind','group'),''))
                if path=='/api/pipeline/model-test':
                    return self.send(200,pipeline.provider_factory(pipeline.settings.public(),pipeline.settings.key()).test())
                if path=='/api/desktop-token':
                    if not website:raise DeskError('配对仅用于网站模式')
                    return self.send(200,website.issue_device_token())
                if path=='/api/pipeline/jobs':return self.send(202,pipeline.create(payload))
                match=re.fullmatch(f'/api/pipeline/jobs/{ID}/(retry|stop|search|read|submit|delete)',path)
                if match:
                    jid,action=match[1],match[2]
                    if action=='retry':return self.send(202,pipeline.retry(jid,limits=payload.get('limits')))
                    if action=='stop':return self.send(200,pipeline.stop(jid))
                    if action=='delete':return self.send(200,pipeline.delete(jid))
                    if action=='submit':return self.send(200,pipeline.submit(jid,payload))
                    return self.send(200,pipeline.retrieve(jid,payload,action=='read'))
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

        def upload(self):
            from .cloud import UPLOAD_LIMIT
            self.close_connection = True
            try:
                if self.headers.get('Content-Type','').split(';')[0] != 'application/octet-stream':
                    return self.send(415, {'error': '上传须为文件内容'})
                size = int(self.headers.get('Content-Length','0'))
                if not 1 <= size <= UPLOAD_LIMIT:
                    return self.send(413, {'error': '单次上传最多200MB'})
                query = parse_qs(urlsplit(self.path).query)
                oid = query.get('object_id',[''])[0]
                filename = query.get('filename',[''])[0]
                with store.connect() as con:store.one(con,'objects',oid)
                self.connection.settimeout(120)
                with tempfile.TemporaryDirectory(prefix='wcwd-receive-') as tmp:
                    archive = Path(tmp)/'upload'
                    with archive.open('wb') as output:
                        remaining = size
                        while remaining:
                            block = self.rfile.read(min(remaining,1024**2))
                            if not block:raise DeskError('上传中断，请重新上传')
                            output.write(block);remaining -= len(block)
                    result = pipeline.upload(archive,filename,oid,query.get('start',[''])[0],query.get('end',[''])[0])
                return self.send(200,result)
            except (DeskError, ValueError, zipfile.BadZipFile, RuntimeError, EOFError):
                return self.send(400, {'error': '上传失败，请核对对象和文件；压缩包须包含 .md，不能含链接、加密文件或超出大小限制'})
            except OSError:
                return self.send(500, {'error': '上传中断或服务器存储不足，请重试'})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.pipeline = pipeline
    server.maintenance_stop = threading.Event()
    original_close=server.server_close
    def close():
        server.maintenance_stop.set();original_close()
    server.server_close=close
    def maintenance():
        while not server.maintenance_stop.wait(60):
            try:
                with pipeline.guard:
                    if not pipeline.running:store.storage_auto()
            except (DeskError,OSError,ValueError):pass  # later retries; no sensitive logs
    threading.Thread(target=maintenance,daemon=True).start()
    return server


def serve(store, port=8770, website_config=None):
    website = None
    pipeline = None
    if website_config:
        from .cloud import WebsiteAccess, CloudPipeline
        website = WebsiteAccess(website_config)
        pipeline = CloudPipeline(store,recover=True)
    server = make_server(store, port, pipeline, website)
    print(f"工作事项台 http://127.0.0.1:{server.server_port}/ （Ctrl+C 关闭）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.maintenance_stop.set()
        server.server_close()
