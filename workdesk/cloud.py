"""Private, single-owner website mode; desktop export remains on the device."""
import base64
import hashlib
import hmac
import json
import secrets
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .flow import Pipeline, config_snapshot
from .flow_config import atomic_json
from .materials import TEXT, IMAGES, OFFICE
from .store import DeskError, now

UPLOAD_LIMIT = 200 * 1024**2
EXPANDED_LIMIT = 512 * 1024**2


class WebsiteAccess:
    """HTTP Basic over HTTPS. Only a salted password hash is stored."""
    def __init__(self, path):
        c = json.loads(Path(path).read_text('utf8'))
        self.origin = c['origin'].rstrip('/')
        if not self.origin.startswith('https://') or '/' in self.origin[8:]:
            raise DeskError('网站地址须为 https:// 主机地址，不含路径')
        self.username = c['username']
        self.salt = bytes.fromhex(c['salt'])
        self.password_hash = bytes.fromhex(c['password_hash'])
        self.lock = threading.Lock()
        self.cache = {}
        self.failures = []
        self.secret = secrets.token_bytes(32)

    def authenticate(self, header):
        if not header or len(header) > 2048:
            return False
        current = time.monotonic()
        tag = hmac.digest(self.secret, header.encode(), 'sha256')
        with self.lock:
            if self.cache.get(tag, 0) > current:
                return True
            self.failures = [t for t in self.failures if t > current - 60]
            if len(self.failures) >= 15:
                return False
            try:
                scheme, token = header.split(' ', 1)
                user, password = base64.b64decode(token, validate=True).decode('utf8').split(':', 1)
                valid = scheme.lower() == 'basic' and hmac.compare_digest(user.encode(), self.username.encode())
                calculated = hashlib.pbkdf2_hmac('sha256', password.encode(), self.salt, 600000)
                valid = valid and hmac.compare_digest(calculated, self.password_hash)
            except (ValueError, UnicodeError):
                valid = False
            if valid:
                self.cache = {tag: current + 900}
            else:
                self.failures.append(current)
            return valid


def initialize_access(path, origin, username, password):
    if len(password) < 12 or not username or ':' in username or len(username) > 100:
        raise DeskError('登录名不能为空；独立的网站密码至少12个字符')
    salt = secrets.token_bytes(32)
    atomic_json(path, dict(origin=origin, username=username, salt=salt.hex(),
                          password_hash=hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000).hex()))
    Path(path).chmod(0o600)
    WebsiteAccess(path)  # validate the origin before it is used


def unpack_upload(archive, destination):
    """Validate *all* members before writing any; preserve relative attachment links."""
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        if not members or len(members) > 10000 or sum(i.file_size for i in members) > EXPANDED_LIMIT:
            raise DeskError('压缩包最多10000项，解压后最多512MB')
        seen = set()
        for i in members:
            name = i.filename.replace('\\', '/')
            p = PurePosixPath(name)
            mode = i.external_attr >> 16
            if (p.is_absolute() or '..' in p.parts or ':' in name or '\x00' in name
                    or not p.parts or stat.S_ISLNK(mode) or i.flag_bits & 1):
                raise DeskError('压缩包含非法路径、链接或加密文件')
            if not i.is_dir() and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise DeskError('压缩包只能包含普通文件和目录')
            key = name.rstrip('/').casefold()
            if key in seen:
                raise DeskError('压缩包含重复路径')
            seen.add(key)
        if not any(PurePosixPath(i.filename).suffix.lower() == '.md' for i in members if not i.is_dir()):
            raise DeskError('压缩包须包含 Markdown 聊天记录')
        for i in members:
            target = destination.joinpath(*PurePosixPath(i.filename.replace('\\', '/')).parts)
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, target.open('xb') as dst:
                count = 0
                while block := src.read(1024**2):
                    count += len(block)
                    if count > i.file_size:
                        raise DeskError('压缩包文件大小不一致')
                    dst.write(block)


class CloudPipeline(Pipeline):
    def sessions(self, refresh=False):
        with self.store.connect() as con:
            objects = [dict(r) for r in con.execute('SELECT * FROM objects ORDER BY name')]
        sessions = [dict(external_id=o['id'], name=o['name'], kind=o['kind']) for o in objects]
        return dict(sessions=sessions, error=None, default_selection=self.settings.public()['last_selection'])

    def upload(self, archive, filename, object_id):
        with self.guard:return self._upload(archive,filename,object_id)

    def _upload(self, archive, filename, object_id):
        with self.store.connect() as con:
            self.store.one(con, 'objects', object_id)
        # Staging is outside data because Store rejects self-imports.
        with tempfile.TemporaryDirectory(prefix='wcwd-upload-') as tmp:
            folder = Path(tmp)
            if filename.lower().endswith('.zip'):
                unpack_upload(archive, folder)
            elif filename.lower().endswith('.md'):
                (folder/'messages.md').write_bytes(Path(archive).read_bytes())
            else:
                raise DeskError('请上传 .md 聊天记录或包含附件的 .zip 压缩包')
            result = self.store.register_import(object_id, folder)
        return result

    def create(self, payload, background=True):
        with self.guard:
            if self.running:
                raise DeskError('已有分析正在运行')
            ids = payload.get('sessions')
            known = {s['external_id']: s for s in self.sessions()['sessions']}
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 50 or
                    any(not isinstance(i, str) or i not in known for i in ids) or len(set(ids)) != len(ids)):
                raise DeskError('请选择1到50个不同的已登记对象')
            mode = payload.get('mode', 'agent')
            if mode not in ('agent', 'api'):
                raise DeskError('网站使用上传资料；无需再次执行 WeFlow 更新')
            supplement = payload.get('supplement', '')
            if not isinstance(supplement, str) or len(supplement) > 2000:
                raise DeskError('补充需求最多2000字')
            cfg = config_snapshot(self.settings)
            if mode == 'api' and not (cfg['base_url'] and cfg['model']):
                raise DeskError('请先配置模型接口')
            goal = cfg['default_goal'] + '\n本轮分析各对象最近一次上传的完整资料；上传时间不是消息时间。' + supplement
            latest_ids = []
            # Keep latest uploads as primary, while frozen history stays available for retrieval.
            with self.store.connect() as con:
                for oid in ids:
                    latest_ids.extend(r[0] for r in con.execute('SELECT s.source_id FROM import_sources s JOIN imports i ON i.id=s.import_id WHERE i.id=(SELECT id FROM imports WHERE object_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1)',(oid,)))
            run = self.store.create_run(ids, goal)
            with self.store.connect() as con:
                for oid in ids:
                    con.execute('INSERT OR IGNORE INTO run_imports SELECT ?,id FROM imports WHERE object_id=?',(run['id'],oid))
            ctx = self.store.context(run['id'])
            primary = [s['id'] for s in ctx['sources'] if s['id'] in latest_ids and Path(s['relative_path']).suffix.lower() == '.md']
            if not primary:
                raise DeskError('所选对象最新资料中没有 Markdown 聊天记录')
            stamp = now()
            j = dict(id=uuid.uuid4().hex, created_at=stamp, mode=mode, start=stamp, end=stamp,
                     selection=[known[i] for i in ids], supplement=supplement, config=cfg,
                     status='queued', phase='等待分析上传资料', conversations=[], warnings=[],
                     run_id=run['id'], attempt=uuid.uuid4().hex, analysis_done=0, analysis_total=0,
                     error=None, ledger={}, usage={}, results=None, cloud_upload=True)
            ctx['pipeline'] = dict(job_id=j['id'], selected_range=[], summary=[], primary_sources=primary,
                                   selected_assets=[s['id'] for s in ctx['sources'] if s['id'] in latest_ids and s['id'] not in primary],
                                   background_sources=[], update_gaps=[])
            folder = self.folder(j['id'])
            atomic_json(folder/'context.json', ctx)
            (folder/'agent-request.md').write_text(
                '# Agent 分析请求\n\n'+goal+'\n\n在服务器的项目目录，使用以下命令：\n'
                f'docker exec wechatworkdesk python -m workdesk.flow --data {self.store.root} context {j["id"]}\n'
                '先读取 docs/agent-workflow.md 和 docs/analysis-flow.md，逐文件报告阅读范围，沿用事项标识。\n'
                f'docker exec wechatworkdesk python -m workdesk.flow --data {self.store.root} submit {j["id"]} --result <容器内结果文件>\n'
                '也可在已登录的网站通过本轮 context、read、search、submit 接口完成同一流程。\n', 'utf8')
            self.save(j)
            self.settings.save({'last_selection': ids})
            if background:
                self._start(j['id'])
            return self.public_job(j['id'])

    def _update(self, j):
        return  # uploaded snapshots already exist

    def _prepare(self, j):
        return

    def _mark_processed(self, j):
        return  # uploads reuse content hashes; desktop message ledgers are independent
