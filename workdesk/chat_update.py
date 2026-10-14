"""Fixed-scope, append-only local WeFlow updates. No workdesk database writes."""
import argparse
import collections
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib import request, parse, error
import uuid

TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
SCOPE = {
    'juziant': '陈华（五瓣桔）', 'lixin031870': '李心', 'qjj1203': '邱以澄',
    'zhengyifen123': '郑祎芬Jane', 'wxid_5wtz5os3axf612': '陈旭锴（陳）',
    'wxid_p0pi4jzr795d51': '黄跃琛（黄岛主）', 'chen-jie03': '陈劼（🌵）',
    '25727595378@chatroom': '和美的休闲专业组', '20007699748@chatroom': '文旅学院中层干部群',
    '20058097294@chatroom': '文旅学院教学管理群', '20387405374@chatroom': '文旅学院通知群',
}
AUDIO = {'.wav', '.mp3', '.silk', '.amr', '.ogg', '.m4a', '.aac', '.pcm', '.flac'}

def digest(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()

def identity(m):
    sid = str(m.get('serverId') or '0')
    if sid != '0':
        return 'server:' + sid
    # localId alone can collide across message databases. Keep time/sort/type/sender.
    fields = ['localId', 'createTime', 'sortSeq', 'localType']
    if not m.get('localId'):
        raise ValueError('消息缺少可靠标识；未写入，等待接口修复')
    return 'local:' + digest(json.dumps([m.get(k) for k in fields], ensure_ascii=False))

def clean(text):
    return html.unescape(re.sub(r'\\([\\`*_{}\[\]()#+.!|>~-])', r'\1', str(text))).strip()

def comparable(text):
    text = clean(text)
    text = re.sub(r'^!?\[[^\n]*?\]\([^\n]*?\)\s*$', '', text, flags=re.M).strip()
    return re.sub(r'^\[(文件|链接|小程序|群公告)\]\s*', r'[\1]', text).lstrip('\x14')

def signature(ts, sender, body):
    return digest(json.dumps([int(ts), sender, clean(body)], ensure_ascii=False))

class Client:
    def __init__(self, config=None, scope=None):
        config = config or Path(os.environ['APPDATA']) / 'weflow/WeFlow-config.json'
        c = json.loads(Path(config).read_text('utf8'))
        if c.get('httpApiHost', '127.0.0.1') not in ('127.0.0.1', 'localhost'):
            raise ValueError('API 必须绑定 127.0.0.1；本程序只连接本机')
        self.base = 'http://127.0.0.1:' + str(int(c.get('httpApiPort', 5031)))
        from .local_token import read_token
        self.token = read_token(c.get('httpApiToken', ''), config)
        self.account = digest(str(c.get('myWxid') or '') + '|' + str(c.get('dbPath') or ''))
        cache = Path(c.get('cachePath') or Path.home()/'Documents/WeFlow') / 'Voices/transcripts.json'
        self.transcripts = {}
        if cache.is_file():
            values = json.loads(cache.read_text('utf8'))
            self.transcripts = {k:v for k,v in values.items() if isinstance(v,str) and
                                any(k.startswith(t + '_') for t in (scope or SCOPE))}
        self.opener = request.build_opener(request.ProxyHandler({}))

    def get(self, path, params=None, binary=False):
        url = self.base + path + ('?' + parse.urlencode(params) if params else '')
        req = request.Request(url, headers={'Authorization': 'Bearer ' + self.token})
        try:
            with self.opener.open(req, timeout=180) as r:
                b = r.read(200 * 1024 * 1024 + 1)
            if len(b) > 200 * 1024 * 1024:
                raise ValueError('附件超过 200MB，未保存')
            if binary:
                return b
            obj = json.loads(b)
            if obj.get('success') is False:
                raise ValueError('WeFlow 返回失败')
            return obj
        except error.HTTPError as e:
            raise RuntimeError(f'WeFlow HTTP {e.code}（请检查接口/数据库连接）') from None
        except error.URLError:
            raise RuntimeError('无法连接 WeFlow；请启动 WeFlow 并开启设置 → API 服务') from None

    def page(self, talker, start, end, offset, limit):
        return self.get('/api/v1/messages', dict(talker=talker, start=start, end=end,
            offset=offset, limit=limit, media=1, image=1, voice=0, video=0, emoji=0))

    def names(self, talker):
        names = {}
        data = self.get('/api/v1/contacts', {'keyword': talker, 'limit': 100})
        for c in data.get('contacts', []):
            if c.get('username') == talker:
                names[talker] = {c.get(k) for k in ('nickname','remark','displayName','alias') if c.get(k)}
        if talker.endswith('@chatroom'):
            for c in self.get('/api/v1/group-members', {'chatroomId': talker}).get('members', []):
                names[c['wxid']] = {c.get(k) for k in ('displayName','nickname','remark','alias','groupNickname') if c.get(k)}
        return names

    def media(self, url):
        u = parse.urlsplit(url)
        if u.netloc != parse.urlsplit(self.base).netloc or not u.path.startswith('/api/v1/media/'):
            raise ValueError('拒绝非本机媒体地址')
        return self.get(u.path, binary=True)

    def transcript(self, talker, m):
        return self.transcripts.get(f"{talker}_{m['createTime']}_{m['localId']}", '')

class Baseline:
    """Conservative exact matches; never infer equivalence from file existence."""
    def __init__(self, texts, talker):
        self.rows = collections.defaultdict(list)
        self.server = {}
        self.files = []
        for p in Path(texts).rglob('*.md'):
            if p.is_symlink():
                continue
            text = p.read_text('utf-8-sig')
            if not re.search(r'会话ID:\s*`' + re.escape(talker) + r'`', text[:1500]):
                continue
            self.files.append(str(p))
            parts = re.split(r'^## (\d{4}\\?-\d{2}\\?-\d{2} \d{2}:\d{2}:\d{2}) (.+)\r?$', text, flags=re.M)
            for i in range(1, len(parts), 3):
                ts = int(datetime.strptime(clean(parts[i]), '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ).timestamp())
                sender, body = clean(parts[i+1]), parts[i+2].strip()
                links = re.findall(r'!?\[([^]]*)\]\(([^)]+)\)', body)
                assets = []
                for label, rel in links:
                    f = (p.parent / parse.unquote(rel)).resolve()
                    if f.is_relative_to(p.parent.resolve()) and f.is_file() and f.suffix.lower() not in AUDIO:
                        assets.append((label, f))
                voice = re.search(r'\[语音转文字\]\s*(.*)', clean(body), re.S)
                row = dict(time=ts, sender=sender, body=body, assets=assets, source=str(p),
                           transcript=voice.group(1).strip() if voice else '')
                self.rows[ts].append(row)
                for _, rel in links:
                    match = re.search(r'voice_.*_\d+_\d+_(\d+)\.', rel)
                    if match:
                        self.server[match.group(1)] = row

    def match(self, m, names, consumed):
        sid = str(m.get('serverId'))
        if sid in self.server:
            return self.server[sid]
        ts = int(m['createTime'])
        labels = names.get(m.get('senderUsername'), set()) | ({'我'} if m.get('isSend') == 1 else set())
        labels = labels | {m.get('senderUsername', '')}
        content = clean(m.get('content') or m.get('parsedContent') or '')
        candidates = []
        for idx, row in enumerate(self.rows[ts]):
            key = (ts, idx)
            typ = int(m.get('localType',0))
            if key in consumed or (row['sender'] not in labels and typ != 10000):
                continue
            equal = content and comparable(content) == comparable(row['body'])
            if typ == 244813135921:
                quoted = re.sub(r'^>[^\n]*\n?', '', row['body'], flags=re.M).strip()
                equal = equal or comparable(content) == comparable(quoted)
            if typ == 10000:
                equal = equal or comparable(content) + '0' == comparable(row['body'])
            if content.startswith('[链接]'):
                title = content[len('[链接]'):].strip()
                labels_in_old = [clean(x) for x in re.findall(r'\[([^]]+)\]\(([^)]+)\)', row['body']) for x in x[:1]]
                equal = equal or title in labels_in_old
            # Existing media names carry localId; time + sender are checked too.
            local = str(m.get('localId'))
            media_match = any(re.match(re.escape(local) + r'_', f.name) for _, f in row['assets'])
            returned_name = str(m.get('mediaFileName') or '')
            if returned_name:
                media_match = media_match or any(f.name.endswith(returned_name) for _,f in row['assets'])
            if equal or (int(m.get('localType', 0)) == 3 and media_match):
                candidates.append((key, row))
        if len(candidates) == 1:
            consumed.add(candidates[0][0])
            return candidates[0][1]
        return None

def write_new(path, data):
    """Exclusive create: old bytes are never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / ('.pending-' + uuid.uuid4().hex)
    try:
        with staging.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(staging, path)  # atomic publish; fails if destination already exists
    finally:
        staging.unlink(missing_ok=True)

class Updater:
    def __init__(self, client, state, export, texts, scope=None, limit=200):
        self.client, self.state, self.export, self.texts = client, Path(state), Path(export), Path(texts)
        self.scope, self.limit = scope or SCOPE, limit

    def run(self, now=None, retry=None, start=None, end=None, progress=None, cancel=None, quiet=False):
        now = now or datetime.now(TZ)
        end = int(now.timestamp()) if end is None else int(end)
        start = end - 7 * 86400 if start is None else int(start)
        if start >= end:
            raise ValueError('开始时间须早于结束时间')
        self.cancel = cancel
        self.state.mkdir(parents=True, exist_ok=True)
        # OS-held lock survives stale files and is released on process termination.
        lock = (self.state / 'update.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close(); raise RuntimeError('另一次更新正在进行，请等待完成') from None
        db = sqlite3.connect(self.state / 'update.sqlite3')
        db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        account = getattr(self.client, 'account', 'simulation')
        previous = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
        if previous and previous[0] != account:
            db.close(); lock.close()
            raise ValueError('WeFlow 当前账号/数据库已变化；请恢复原账号，避免跨账号混合导出')
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('account',?)", (account,))
        db.commit()
        db.execute('CREATE TABLE IF NOT EXISTS messages (talker TEXT, key TEXT, payload TEXT, old_source TEXT, PRIMARY KEY(talker,key))')
        report = dict(start=datetime.fromtimestamp(start,TZ).isoformat(), end=datetime.fromtimestamp(end,TZ).isoformat(), conversations=[])
        try:
            for talker, name in self.scope.items():
                self._check_cancel()
                if retry and talker not in retry:
                    continue
                result = dict(external_id=talker, name=name, added=0, skipped=0, outside=0,
                              transcription_gaps=0, attachment_gaps=0, error=None, old_format_overlap=0)
                report['conversations'].append(result)
                try:
                    self._session(db, talker, start, end, result)
                except Exception as e:
                    self._check_cancel()
                    # Do not include request URLs, credential values, or message contents.
                    result['error'] = str(e) if isinstance(e, (RuntimeError, ValueError)) else type(e).__name__
                if progress:
                    progress(result)
                if not quiet:
                    print(f"{name}: 新增 {result['added']}，跳过 {result['skipped']}，语音缺口 {result['transcription_gaps']}，附件缺口 {result['attachment_gaps']}" + (f"，失败：{result['error']}" if result['error'] else ''), flush=True)
            path = self.state / ('report-' + uuid.uuid4().hex + '.json')
            write_new(path, json.dumps(report, ensure_ascii=False, indent=2).encode('utf8'))
            report['report_path'] = str(path)
            return report
        finally:
            db.close(); lock.close()

    def _session(self, db, talker, start, end, result):
        base = Baseline(self.texts, talker)
        names = self.client.names(talker)
        consumed = set()
        folder = self.export / 'increments' / talker
        folder.mkdir(parents=True, exist_ok=True)
        # Recover a crash after file create but before index commit.
        for path in folder.glob('*.md'):
            text = path.read_text('utf8')
            marker = re.search(r'<!-- message-key: (.*?) -->', text)
            if marker:
                db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?)', (talker,marker[1], '{}',str(path)))
        db.commit()
        offset, pages = 0, set()
        inventory = {}
        while True:
            self._check_cancel()
            page = self.client.page(talker, start, end, offset, self.limit)
            if page.get('talker') != talker or not isinstance(page.get('messages'), list):
                raise ValueError('接口会话标识或消息格式不匹配')
            rows = page['messages']
            fingerprint = digest(json.dumps(rows, sort_keys=True, ensure_ascii=False))
            if rows and fingerprint in pages:
                raise ValueError('接口重复返回同一页，已停止；可重试')
            pages.add(fingerprint)
            for m in rows:
                ts = int(m['createTime'])
                if not start <= ts <= end:
                    result['outside'] += 1; continue
                key = identity(m)
                old = base.match(m, names, consumed)
                saved = db.execute('SELECT old_source FROM messages WHERE talker=? AND key=?', (talker,key)).fetchone()
                source = old['source'] if old else (saved[0] if saved else str(folder / (str(ts) + '-' + digest(key) + '.md')))
                inventory[key] = dict(time=ts,source=source,transcript_cached=bool(self._transcript(talker,m,old)),
                    attachments=[str(f) for _,f in old['assets']] if old else [],
                    possible_old_format_overlap=bool(not old and base.rows[ts]),
                    body=old['body'] if old else str(m.get('content') or m.get('parsedContent') or ''),
                    sender='我' if m.get('isSend') == 1 else m.get('senderUsername') or '发送者未知',
                    kind=int(m.get('localType',0)), voice_text=self._transcript(talker,m,old), quote=m.get('quote'))
                if not old and m.get('mediaFileName'):
                    asset=folder/'media'/(digest(key)+Path(m['mediaFileName']).suffix.lower())
                    inventory[key]['attachments']=[str(asset)]
                if saved and (saved[0] == 'baseline' or Path(saved[0]).is_file()):
                    result['skipped'] += 1
                    if old and saved[0] != 'baseline':
                        result['old_format_overlap'] += 1
                    transcript = self._transcript(talker,m,old)
                    self._gaps(m, old, result, transcript)
                    if saved[0] != 'baseline':
                        previous = Path(saved[0]).read_text('utf8')
                        if '[图片附件缺口' in previous:
                            if m.get('mediaUrl'):
                                supplement = folder / (str(ts) + '-' + digest(key) + '-attachment.md')
                                if not supplement.exists():
                                    body = self._render(m, talker, key + ':attachment', folder, old, result)
                                    write_new(supplement, body.encode('utf8'))
                            else:
                                result['attachment_gaps'] += 1
                        if '[语音转写缺口' in previous and transcript:
                            supplement = folder / (str(ts) + '-' + digest(key) + '-transcript.md')
                            if not supplement.exists():
                                body = self._render(m, talker, key + ':transcript', folder, old, result)
                                write_new(supplement,body.encode('utf8'))
                    continue
                if old:
                    db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)', (talker,key,'{}','baseline')); db.commit()
                    result['skipped'] += 1
                    self._gaps(m,old,result,self._transcript(talker,m,old))
                    continue
                text = self._render(m, talker, key, folder, old, result)
                path = folder / (str(ts) + '-' + digest(key) + '.md')
                if not path.exists():
                    write_new(path, text.encode('utf8'))
                    result['added'] += 1
                else:
                    result['skipped'] += 1
                db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)', (talker,key,'{}',str(path))); db.commit()
            offset += len(rows)
            if not page.get('hasMore'):
                break
            if not rows:
                raise ValueError('接口 hasMore=true 但消息页为空；未标记完成')
        inventory_path = folder / ('inventory-' + uuid.uuid4().hex + '.json')
        write_new(inventory_path, json.dumps(dict(external_id=talker,start=start,end=end,messages=inventory),ensure_ascii=False,indent=2).encode('utf8'))
        result['inventory'] = str(inventory_path)

    def _check_cancel(self):
        if getattr(self,'cancel',None) and self.cancel.is_set():
            raise RuntimeError('更新已停止，已保存消息保留')

    def _transcript(self, talker, m, old):
        return (old['transcript'] if old else '') or (self.client.transcript(talker,m) if hasattr(self.client,'transcript') else '')

    def _gaps(self, m, old, result, transcript=''):
        if int(m.get('localType',0)) == 34 and not transcript:
            result['transcription_gaps'] += 1
        if str(m.get('content','')).startswith('[文件]') and not (old and old['assets']):
            result['attachment_gaps'] += 1

    def _render(self, m, talker, key, folder, old, result):
        typ = int(m.get('localType',0)); sender = '我' if m.get('isSend') == 1 else m.get('senderUsername') or '发送者未知'
        stamp = datetime.fromtimestamp(int(m['createTime']),TZ).isoformat()
        body = str(m.get('content') or m.get('parsedContent') or m.get('rawContent') or '')
        if typ == 34:
            transcript = self._transcript(talker,m,old)
            if transcript:
                body = '[语音转文字] ' + transcript
            else:
                body += '\n\n[语音转写缺口：接口未提供已核验转写，未导出音频]'
                result['transcription_gaps'] += 1
        if typ == 3:
            url = m.get('mediaUrl')
            if url:
                ext = Path(m.get('mediaFileName') or '').suffix.lower()
                if ext not in {'.png','.jpg','.jpeg','.gif','.webp','.bmp'}:
                    raise ValueError('图片扩展名不受支持')
                asset = folder / 'media' / (digest(key)+ext)
                if not asset.exists():
                    write_new(asset, self.client.media(url))
                body += f'\n\n![图片](media/{asset.name})'
            else:
                body += '\n\n[图片附件缺口：WeFlow 未返回可用图片]'
                result['attachment_gaps'] += 1
        if typ in (34359738417,103079215153,25769803825) or body.startswith('[文件]') or (typ == 49 and re.search(r'<type>\s*6\s*</type>', str(m.get('rawContent','')))):
            body += '\n\n[文件附件缺口：此版本 HTTP 接口没有文件导出路由；保留消息，原导出文件不变]'
            result['attachment_gaps'] += 1
        return f'# {self.scope[talker]}\n\n- 会话ID: `{talker}`\n- 消息标识: `{key}`\n- 来源: WeFlow 本机 HTTP 接口\n- 北京时间: {stamp}\n\n<!-- message-key: {key} -->\n\n## {stamp} {sender}\n\n{body}\n'

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf8')
    parser = argparse.ArgumentParser(description='固定11会话最近7天增量更新')
    parser.add_argument('--retry-failed', type=Path, help='上次报告路径；仅重试失败会话')
    parser.add_argument('--retry-latest', action='store_true', help='只重试最近报告中的失败会话')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        retry = None
        if args.retry_latest:
            reports = list((root/'data/chat-update').glob('report-*.json'))
            if not reports:
                raise ValueError('尚无更新报告；请先运行更新工作聊天.cmd')
            args.retry_failed = max(reports, key=lambda p:p.stat().st_mtime_ns)
        if args.retry_failed:
            retry = {r['external_id'] for r in json.loads(args.retry_failed.read_text('utf8'))['conversations'] if r['error']}
            if not retry:
                print('报告中没有失败会话'); return 0
            if not retry <= SCOPE.keys():
                raise ValueError('重试报告含未授权会话')
        from .flow_config import Settings
        paths=Settings(root/'data').public()['device_paths']
        client = Client(config=Path(paths['weflow_config']))
        client.get('/health')
        from .device_paths import update_state
        export=Path(paths['export_directory'])
        updater = Updater(client, update_state(root/'data',export), export, export/'texts')
        report = updater.run(retry=retry)
        print('报告：' + report['report_path'])
        return 1 if any(c['error'] for c in report['conversations']) else 0
    except (RuntimeError, ValueError) as e:
        print(str(e)); return 1

if __name__ == '__main__':
    raise SystemExit(main())
