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
    def analysis_scope(self):
        path=self.store.root/'flow/analysis-scope.json'
        return json.loads(path.read_text('utf8')) if path.exists() else {}
    def sessions(self, refresh=False):
        with self.store.connect() as con:
            objects = [dict(r) for r in con.execute('SELECT * FROM objects ORDER BY name')]
        with self.store.connect() as con:
            uploaded={r[0]:r[1] for r in con.execute("SELECT object_id,max(created_at) FROM imports i WHERE EXISTS(SELECT 1 FROM import_sources x JOIN sources s ON s.id=x.source_id WHERE x.import_id=i.id AND s.relative_path NOT LIKE 'analysis/%') GROUP BY object_id")}
        sessions = [dict(external_id=o['id'], name=o['name'], kind=o['kind'],uploaded_at=uploaded.get(o['id'])) for o in objects]
        return dict(sessions=sessions, error=None, default_selection=self.settings.public()['last_selection'])

    def upload(self, archive, filename, object_id, start='',end=''):
        with self.guard:return self._upload(archive,filename,object_id,start,end)

    def _upload(self, archive, filename, object_id,start='',end=''):
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
            result = self.store.register_import(object_id, folder,start,end)
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
            from .analysis_range import bounds,freeze_window
            window=bounds(payload);summary=[];warnings=[];range_primary=[];range_assets=[]
            from .analysis_cache import signature,load
            cache=load(self.root);candidates={};old_by_object={}
            incremental=cfg['incremental_analysis'] and not payload.get('force_analysis',False)
            for oid in ids:
                fingerprint=signature(self.store,oid,cfg,supplement)
                old_by_object[oid]=cache.get(oid+':'+fingerprint,{}) if incremental else {}
                if old_by_object[oid]:warnings.extend(old_by_object[oid].get('warnings',[]))
                candidates[oid]={'signature':fingerprint,'messages':{},'sources':{}}
            if window:
                for oid in ids:
                    primary,assets,item,notes=freeze_window(self.store,oid,*window,processed=old_by_object[oid],allow_empty=True)
                    if not item['total_messages'] and not item.get('reused_sources') and not primary:
                        warnings.append(known[oid]['name']+'：所选时段无可用消息，本轮跳过。')
                    candidates[oid]['messages']=item.pop('message_marks',{})
                    range_primary.extend(primary);range_assets.extend(assets);summary.append(item);warnings.extend(notes)
            if window and not range_primary and not any(x.get('reused_messages') or x.get('reused_sources') for x in summary):
                raise DeskError('所选对象在分析时段内没有可用聊天记录，请先更新该时段或调整范围')
            goal = cfg['default_goal'] + ('\n只从本轮消息时段提出新增事项：'+window[0].isoformat()+' 至 '+window[1].isoformat()+'。历史资料和已确认记忆用于理解、回查与沿用原事项；不要把范围外的旧通知重新当作新增事项。' if window else '\n本轮分析各对象最近一次上传的完整资料；上传时间不是消息时间。') + supplement
            latest_ids = []
            # Keep latest uploads as primary, while frozen history stays available for retrieval.
            with self.store.connect() as con:
                for oid in ids:
                    latest_ids.extend(r[0] for r in con.execute("SELECT s.source_id FROM import_sources s JOIN imports i ON i.id=s.import_id WHERE i.id=(SELECT i2.id FROM imports i2 WHERE i2.object_id=? AND EXISTS(SELECT 1 FROM import_sources x JOIN sources y ON y.id=x.source_id WHERE x.import_id=i2.id AND y.relative_path NOT LIKE 'analysis/%') ORDER BY i2.created_at DESC,i2.rowid DESC LIMIT 1)",(oid,)))
            run = self.store.create_run(ids, goal)
            with self.store.connect() as con:
                for oid in ids:
                    con.execute('INSERT OR IGNORE INTO run_imports SELECT ?,id FROM imports WHERE object_id=?',(run['id'],oid))
            ctx = self.store.context(run['id'])
            primary = [s['id'] for s in ctx['sources'] if s['id'] in latest_ids and not s['relative_path'].startswith(('background/','analysis/')) and Path(s['relative_path']).suffix.lower() == '.md']
            available_primary=bool(primary or range_primary or any(x.get('reused_messages') or x.get('reused_sources') for x in summary))
            if window:primary=range_primary
            if not window and incremental:
                before=len(primary)
                primary=[sid for sid in primary if not any(s['id']==sid and old_by_object[s['object_id']].get('sources',{}).get(s['relative_path'])==s['sha256'] for s in ctx['sources'])]
                if before>len(primary):warnings.append('已发布且内容未变的聊天资料已复用，无需再次调用模型。')
            if not available_primary:
                raise DeskError('所选对象最新资料中没有 Markdown 聊天记录')
            stamp = now()
            j = dict(id=uuid.uuid4().hex, created_at=stamp, mode=mode, start=window[0].isoformat() if window else stamp, end=window[1].isoformat() if window else stamp,
                     selection=[known[i] for i in ids], supplement=supplement, config=cfg,
                     status='queued', phase='等待分析资料', conversations=[], warnings=warnings,
                     run_id=run['id'], attempt=uuid.uuid4().hex, analysis_done=0, analysis_total=0,
                     error=None, ledger={}, usage={}, results=None, cloud_upload=True,cache_candidates=candidates,budget_start={'calls':0,'tokens':0})
            assets=range_assets if window else [s['id'] for s in ctx['sources'] if s['id'] in latest_ids and s['id'] not in primary and Path(s['relative_path']).suffix.lower()!='.md' and not s['relative_path'].startswith('background/')]
            if incremental:
                assets=[sid for sid in assets if not any(s['id']==sid and old_by_object[s['object_id']].get('sources',{}).get(s['relative_path'])==s['sha256'] for s in ctx['sources'])]
            for s in ctx['sources']:
                if s['id'] in primary+assets:candidates[s['object_id']]['sources'][s['relative_path']]=s['sha256']
            reused=sum(x.get('reused_messages',0) for x in summary)
            if reused:warnings.append(f'复用已发布分析的 {reused} 条相同消息；新增消息、转写或附件变化仍会分析。')
            ctx['pipeline'] = dict(job_id=j['id'], selected_range=[j['start'],j['end']] if window else [], summary=summary, primary_sources=primary,
                                   selected_assets=assets,
                                   background_sources=[s['id'] for s in ctx['sources'] if s['relative_path'].startswith('background/') and not s.get('cleaned_at')], update_gaps=[])
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
            atomic_json(self.store.root/'flow/analysis-scope.json',{k:payload[k] for k in ('sessions','range','start','end') if k in payload})
            if background:
                self._start(j['id'])
            return self.public_job(j['id'])

    def _update(self, j):
        return  # uploaded snapshots already exist

    def _prepare(self, j):
        self._check_cache_signature(j)
        ctx=self.context(j['id'])
        if not ctx['pipeline']['primary_sources'] and not ctx['pipeline']['selected_assets']:
            result={'matters':[],'backgrounds':[],'coverage':[{'source_id':s['id'],'state':'unread','note':'本轮复用已发布分析，没有重新读取原文件。'} for s in ctx['sources']]}
            j['results']=self.store.submit(j['run_id'],result);self._mark_processed(j)
            j.update(status='completed',phase='相同资料已复用，本轮无需再次分析');self.save(j)

    def _analyse(self,j):
        self._check_cache_signature(j)
        return super()._analyse(j)

    def _check_cache_signature(self,j):
        from .analysis_cache import signature
        for oid,c in j.get('cache_candidates',{}).items():
            if c['signature']!=signature(self.store,oid,j['config'],j['supplement']):
                raise DeskError('准备资料后工作背景或人工反馈已改变，请新建分析以重新核查已跳过的消息；本轮进度仍保留。')

    def _mark_processed(self, j):
        from .analysis_cache import published,signature
        if not j.get('cache_candidates') and j.get('cloud_upload'):
            # Upgrade a legacy completed task using only its frozen authorized snapshots.
            from datetime import datetime
            from .analysis_range import freeze_window
            ctx=self.context(j['id']);authorized={s['id'] for s in ctx['sources']};candidates={}
            for obj in ctx['objects']:
                oid=obj['id']
                try:_,_,summary,_=freeze_window(self.store,oid,datetime.fromisoformat(j['start']),datetime.fromisoformat(j['end']),source_ids=authorized,register=False)
                except DeskError:continue
                candidates[oid]={'signature':signature(self.store,oid,j['config'],j['supplement']),'messages':summary['message_marks'],'sources':{}}
            j['cache_candidates']=candidates
        if j.get('cache_candidates'):
            with self.guard:
                ctx=self.context(j['id']);states={}
                with self.store.connect() as con:
                    states={r[0]:r[1] for r in con.execute('SELECT source_id,state FROM coverage WHERE run_id=?',(j['run_id'],))}
                sources={s['id']:s for s in ctx['sources']};candidates={}
                for oid,c in j['cache_candidates'].items():
                    if c['signature']!=signature(self.store,oid,j['config'],j['supplement']):continue
                    primary=[sid for sid in ctx['pipeline']['primary_sources'] if sources[sid]['object_id']==oid and not sources[sid]['relative_path'].startswith('background/')]
                    # An Agent may publish partial reading: never mark its unread messages processed.
                    candidate={**c,'messages':c['messages'] if all(states.get(sid)=='read' for sid in primary) else {},'sources':{},
                               'warnings':[w for w in j['warnings'] if not w.startswith(('复用','已发布'))]}
                    for sid in ctx['pipeline']['primary_sources']+ctx['pipeline']['selected_assets']:
                        s=sources[sid]
                        canonical=j.get('asset_aliases',{}).get(sid)
                        if canonical and s['object_id']==oid and (states.get(canonical)=='read' or canonical in j.get('ledger',{}) and Path(s['relative_path']).suffix.lower() in IMAGES):
                            candidate['sources'][s['relative_path']]=s['sha256']
                        if s['object_id']==oid and states.get(sid)=='read':candidate['sources'][s['relative_path']]=s['sha256']
                        elif s['object_id']==oid and sid in j.get('ledger',{}) and Path(s['relative_path']).suffix.lower() in IMAGES:
                            candidate['sources'][s['relative_path']]=s['sha256']
                    candidates[oid]=candidate
                published(self.root,candidates,j['run_id'])
