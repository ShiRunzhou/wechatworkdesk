"""Device-only export and upload assistant. No local matter database."""
import base64
import copy
import json
import os
import re
import threading
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request, parse, error

from .chat_update import Client, Updater, SCOPE, TZ, AUDIO
from .flow_config import atomic_json
from .model_api import NoRedirect

WEB = Path(__file__).parent / 'web'


class VoiceClient(Client):
    def __init__(self, config, scope, stt, key, cache):
        super().__init__(config,scope);self.stt=stt;self.stt_key=key;self.cache=Path(cache)/self.account
        self.cache.mkdir(parents=True,exist_ok=True)
        self.local_voice=None
        if stt.get('voice_mode')=='local':
            from .local_voice import LocalVoice
            self.local_voice=LocalVoice(stt)
    def page(self,talker,start,end,offset,limit):
        page=self.get('/api/v1/messages',dict(talker=talker,start=start,end=end,offset=offset,limit=limit,media=1,image=1,voice=1,video=0,emoji=0))
        for m in page.get('messages',[]):
            if int(m.get('localType',0))!=34:continue
            from .chat_update import digest,identity
            key=f"{talker}_{m['createTime']}_{m['localId']}";p=self.cache/(digest(talker+identity(m))+'.json')
            if p.exists():self.transcripts[key]=json.loads(p.read_text('utf8'))['text']
            if self.transcripts.get(key) or not m.get('mediaUrl'):continue
            try:
                audio=self.media(m['mediaUrl']);name=Path(m.get('mediaFileName') or 'voice.wav').name
                if not audio or len(audio)>25*1024**2:continue
                if getattr(self,'local_voice',None):
                    text=self.local_voice.transcribe(audio)
                    if text:
                        self.transcripts[key]=text;atomic_json(p,{'text':text,'engine':'sensevoice-local'})
                    continue
                boundary='wcwd'+uuid.uuid4().hex
                if '\r' in name or '\n' in name or '"' in name:continue
                parts=[f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{self.stt["stt_model"]}\r\n'.encode(),
                       f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),audio,f'\r\n--{boundary}--\r\n'.encode()]
                headers={'Content-Type':'multipart/form-data; boundary='+boundary}
                if self.stt_key:headers['Authorization']='Bearer '+self.stt_key
                req=request.Request(self.stt['stt_url'].rstrip('/')+'/audio/transcriptions',data=b''.join(parts),headers=headers)
                with request.build_opener(request.ProxyHandler({}),NoRedirect()).open(req,timeout=180) as response:
                    raw=response.read(2*1024**2+1)
                if len(raw)>2*1024**2:continue
                text=json.loads(raw).get('text','')
                if isinstance(text,str) and text.strip():
                    self.transcripts[key]=text.strip();atomic_json(p,{'text':text.strip()})
            except (ValueError,OSError,RuntimeError):pass # The exporter records a visible transcription gap.
        return page


def valid_talker(t):
    return isinstance(t, str) and bool(re.fullmatch(r'[A-Za-z0-9_.@-]{1,180}', t)) and '..' not in t


def time_range(payload, clock=None):
    end = clock or datetime.now(TZ)
    now_limit=end
    if payload.get('range', 'recent') == 'custom':
        try:
            start = datetime.strptime(payload['start'], '%Y/%m/%d').replace(tzinfo=TZ)
            end = datetime.strptime(payload['end'], '%Y/%m/%d').replace(tzinfo=TZ) + timedelta(days=1, seconds=-1)
            if end.date()>now_limit.date():raise ValueError()
            end=min(end,now_limit)
        except (ValueError, KeyError, TypeError):
            raise ValueError('日期须为 yyyy/mm/dd，请从日历选择') from None
    else:
        days = payload.get('days', 7)
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError('最近天数须为1到365')
        start = end - timedelta(days=days)
    if start > end or end-start > timedelta(days=366):
        raise ValueError('时间范围无效或超过一年')
    return int(start.timestamp()), int(end.timestamp())


class Remote:
    def __init__(self, config, password):
        self.base = config['website'].rstrip('/')
        u = parse.urlsplit(self.base)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
            raise ValueError('网站地址须为 https:// 主机地址')
        self.headers = {'Authorization': 'Basic '+base64.b64encode((config['username']+':'+password).encode()).decode(), 'Origin': self.base}

    def call(self, path, payload=None, file=None):
        headers = dict(self.headers)
        body = None
        if file:
            body = Path(file).read_bytes(); headers['Content-Type'] = 'application/octet-stream'
        elif payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode();headers['Content-Type']='application/json'
        req = request.Request(self.base+path, data=body, headers=headers)
        opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=180) as r:
                raw = r.read(32*1024**2+1)
            if len(raw)>32*1024**2:raise ValueError('网站返回内容过大')
            return json.loads(raw)
        except error.HTTPError as e:
            raise ValueError(f'网站返回 HTTP {e.code}，请检查登录信息或上传限制') from None
        except (error.URLError, TimeoutError, ConnectionError):
            # A POST may have arrived: never automatically repeat it.
            raise ValueError('网站连接失败；未自动重复上传，请检查网络后重试') from None


def package_inventory(inventory, output, export):
    """Freeze exactly this window, including skipped local messages and supplements."""
    data=json.loads(Path(inventory).read_text('utf8'));root=Path(export).resolve()
    lines=[f'# 聊天记录：{data["external_id"]}',f'会话ID: `{data["external_id"]}`',
           '范围: '+datetime.fromtimestamp(data['start'],TZ).isoformat()+' 至 '+datetime.fromtimestamp(data['end'],TZ).isoformat(), '']
    assets={};gaps=0
    for key,m in sorted(data['messages'].items(),key=lambda x:(x[1]['time'],x[0])):
        lines += [f'## {datetime.fromtimestamp(m["time"],TZ).isoformat()} {m.get("sender", "")}', '消息标识: '+key, str(m.get('body',''))]
        if m.get('voice_text'):lines.append('[语音转文字] '+m['voice_text'])
        elif m.get('kind')==34:lines.append('[语音文字缺失：不得推测语音内容]')
        if m.get('quote'):lines.append('引用资料: '+json.dumps(m['quote'],ensure_ascii=False))
        for raw in m.get('attachments',[]):
            p=Path(raw)
            if p.is_symlink() or not p.resolve().is_relative_to(root):raise ValueError('附件超出导出目录')
            if p.suffix.lower() in AUDIO:continue
            if not p.is_file():lines.append('[附件缺失] '+p.name);gaps+=1;continue
            import hashlib
            name='attachments/'+hashlib.sha256(str(p.resolve()).encode()).hexdigest()[:16]+p.suffix.lower()
            assets[name]=p;lines.append(f'[附件]({name})')
        lines.append('')
    if not data['messages']:lines.append('本轮所选时间没有消息。')
    if len(assets)+1>10000:raise ValueError('本次附件过多，请缩短时间范围')
    text='\n'.join(lines).encode('utf8')
    if len(text)+sum(p.stat().st_size for p in assets.values())>512*1024**2:raise ValueError('资料超过512MB，请缩短时间范围')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('messages.md',text)
        for name,p in assets.items():z.write(p,name)
    if Path(output).stat().st_size>200*1024**2:raise ValueError('压缩包超过200MB，请缩短时间范围')
    return gaps


class Assistant:
    def __init__(self, root, client_factory=Client, remote_factory=Remote):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'settings.json';self.lock=threading.RLock();self.running=False
        self.client_factory=client_factory;self.remote_factory=remote_factory;self.password='';self.stt_key='';self.catalog={};self.objects=[]
        self.config={'website':'https://desk.plantisland.cn','username':'shirunzhou',
            'weflow_config':str(Path(os.environ.get('APPDATA',str(Path.home())))/'weflow/WeFlow-config.json'),
            'export_directory':str(Path('E:/wechatrecord') if Path('E:/wechatrecord').is_dir() else Path.home()/'Documents/WeChatWorkDesk/wechatrecord'),
            'selection':list(SCOPE),'presets':{},'mapping':{},'stt_url':'','stt_model':'',
            'voice_mode':'local','voice_model_directory':'','weflow_executable':''}
        if self.path.exists():self.config.update(json.loads(self.path.read_text('utf8')))
        if not self.config['weflow_executable']:
            from .device_paths import load_paths
            from types import SimpleNamespace
            self.config['weflow_executable']=load_paths(SimpleNamespace(path=self.root.parent/'flow/settings.json'))['weflow_executable']
        cache=self.root/'contacts.json'
        if cache.exists():
            data=json.loads(cache.read_text('utf8'));self.catalog=data.get('catalog',{});self.objects=data.get('objects',[])
        self.jobs_dir=self.root/'jobs';self.jobs_dir.mkdir(exist_ok=True);self._job_cache={}
        for p in self.jobs_dir.glob('*.json'):
            j=json.loads(p.read_text('utf8'))
            if j['status']=='running':j['status']='interrupted';j['phase']='程序重启，可重试未完成部分';atomic_json(p,j)
            self._job_cache[j['id']]=j

    def public(self):
        from .local_voice import status
        with self.lock:
            return copy.deepcopy({'config':self.config,'sessions':list(self.catalog.values()),'objects':self.objects,
                'password_set':bool(self.password),'voice_status':status(self.config),'jobs':sorted((j for j in self._job_cache.values() if not j.get('deleted_at')),key=lambda j:j['created_at'],reverse=True)[:30]})

    def delete_job(self,p):
        with self.lock:
            j=self._job_cache.get(p.get('id'))
            if not j:raise ValueError('任务不存在')
            if j['status'] in ('running','queued'):raise ValueError('请等待更新结束后再删除记录')
            j['deleted_at']=datetime.now(TZ).isoformat()
            self.save_job(j)
            return {'deleted':True}

    def save(self,p):
        with self.lock:
            if self.running:raise ValueError('更新进行中，请完成后更改设置')
            for k in ('website','username','weflow_config','export_directory'):
                if k not in p:continue
                if not isinstance(p[k],str) or not p[k].strip() or len(p[k])>2000:raise ValueError('请填写有效设置')
            candidate={**self.config,**{k:p[k].strip() for k in ('website','username','weflow_config','export_directory') if k in p}}
            if 'voice_mode' in p:
                if p['voice_mode'] not in ('local','api','cache'):raise ValueError('转写模式无效')
                candidate['voice_mode']=p['voice_mode']
            for k in ('voice_model_directory','weflow_executable'):
                if k in p:
                    if not isinstance(p[k],str) or len(p[k])>2000:raise ValueError('模型或程序路径无效')
                    value=p[k].strip()
                    if value and not Path(value).is_absolute():raise ValueError('请填写本机完整路径，留空则自动识别')
                    candidate[k]=value
            for k in ('stt_url','stt_model'):
                if k in p:
                    if not isinstance(p[k],str) or len(p[k])>2000 or '\n' in p[k] or '\r' in p[k]:raise ValueError('转写配置无效')
                    candidate[k]=p[k].strip()
            if candidate['stt_url']:
                u=parse.urlsplit(candidate['stt_url'])
                if not u.hostname or u.username or u.password or u.query or u.fragment or (u.scheme!='https' and not(u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1'))):raise ValueError('转写接口须为HTTPS或本机HTTP地址')
            Remote(candidate,'')
            for k in ('weflow_config','export_directory'):
                if not Path(candidate[k]).is_absolute():raise ValueError('请填写本机完整路径')
            dest=Path(candidate['export_directory']).resolve()
            if dest.is_relative_to(self.root.resolve()) or self.root.resolve().is_relative_to(dest):raise ValueError('导出目录与助手数据目录不能互相包含')
            if ':' in candidate['username']:raise ValueError('登录名不能包含冒号')
            for key in ('password','stt_key'):
                if key in p and (not isinstance(p[key],str) or len(p[key])>2000):raise ValueError('凭据格式无效')
            if 'password' in p and p['password']:self.password=p['password']
            if p.get('stt_key'):self.stt_key=p['stt_key']
            if 'mapping' in p:
                if not isinstance(p['mapping'],dict) or any(not valid_talker(t) or not isinstance(v,str) or (v and not re.fullmatch('[a-f0-9]{32}',v)) for t,v in p['mapping'].items()):raise ValueError('网站对象对应关系无效')
                candidate['mapping']=p['mapping']
            self.config=candidate;atomic_json(self.path,self.config)
            return self.public()

    def refresh(self):
        with self.lock:
            if self.running:raise ValueError('更新进行中，请稍后刷新')
            client=self.client_factory(self.config['weflow_config'],scope=self.config['selection'])
            values={}
            for route,field in [('/api/v1/contacts','contacts'),('/api/v1/sessions','sessions')]:
                data=client.get(route,{'limit':10000})
                for c in data.get(field,[]):
                    t=c.get('username','')
                    if valid_talker(t):values[t]={'external_id':t,'name':c.get('remark') or c.get('displayName') or c.get('nickname') or t,'kind':'group' if t.endswith('@chatroom') else 'person'}
            self.catalog=values
            atomic_json(self.root/'contacts.json',{'catalog':self.catalog,'objects':self.objects})
            return self.public()

    def connect(self):
        with self.lock:
            if self.running:raise ValueError('更新进行中，请稍后检查')
            state=self.remote_factory(self.config,self.password).call('/api/state')
            self.objects=state['objects']
            for t,s in self.catalog.items():
                exact=[o for o in self.objects if o.get('external_id')==t]
                if len(exact)==1 and not self.config['mapping'].get(t):self.config['mapping'][t]=exact[0]['id']
            atomic_json(self.path,self.config)
            atomic_json(self.root/'contacts.json',{'catalog':self.catalog,'objects':self.objects})
            return self.public()

    def preset(self,p):
        with self.lock:
            name=p.get('name','').strip();ids=p.get('selection',[])
            if not name or len(name)>50 or not isinstance(ids,list) or not ids or any(t not in self.catalog for t in ids):raise ValueError('请填写范围名称并选择联系人')
            self.config['presets'][name]=list(dict.fromkeys(ids));atomic_json(self.path,self.config)
            return self.public()

    def save_job(self,j):
        with self.lock:
            self._job_cache[j['id']]=copy.deepcopy(j)
            atomic_json(self.jobs_dir/(j['id']+'.json'),j)

    def start(self,p,background=True):
        with self.lock:
            if self.running:raise ValueError('另一次更新正在进行')
            if p.get('retry'):
                jid=p['retry']
                if not re.fullmatch('[a-f0-9]{32}',str(jid)):raise ValueError('任务编号无效')
                if jid not in self._job_cache:raise ValueError('未找到任务')
                j=copy.deepcopy(self._job_cache[jid])
                if j['status']=='completed':raise ValueError('本次更新已经完成')
            else:
                selected=p.get('selection',[])
                if not isinstance(selected,list) or not 1<=len(selected)<=50 or len(set(selected))!=len(selected) or any(t not in self.catalog for t in selected):raise ValueError('请选择1到50个联系人或群')
                start,end=time_range(p);mode=p.get('mode','upload')
                if mode not in ('export','upload','api','agent'):raise ValueError('完成后操作无效')
                if mode!='export' and not self.password:raise ValueError('请先填写网站登录密码并检查连接')
                j={'id':uuid.uuid4().hex,'created_at':datetime.now(TZ).isoformat(),'start':start,'end':end,'selection':[self.catalog[t] for t in selected],
                   'mode':mode,'export_directory':self.config['export_directory'],'weflow_config':self.config['weflow_config'],
                   'website':self.config['website'],'username':self.config['username'],'status':'running','phase':'开始更新','conversations':[],'analysis_job':None}
            if j['website']!=self.config['website'] or j['username']!=self.config['username']:raise ValueError('重试须使用原网站和账号')
            j['status']='running';self.running=True
            try:self.save_job(j)
            except OSError:
                self.running=False;j.update(status='partial',phase='任务日志无法保存，请检查文件占用或磁盘权限后重试')
                self._job_cache[j['id']]=copy.deepcopy(j)
                raise
            if background:threading.Thread(target=self.run,args=(j,),daemon=True).start()
            else:self.run(j)
            return copy.deepcopy(j)

    def run(self,j):
        try:
            folder=self.jobs_dir/j['id'];folder.mkdir(exist_ok=True)
            remote=self.remote_factory(self.config,self.password) if j['mode']!='export' else None
            for s in j['selection']:
                t=s['external_id'];row=next((r for r in j['conversations'] if r['external_id']==t),None)
                if row and row.get('uploaded'):continue
                if row is None:row={'external_id':t,'name':s['name']};j['conversations'].append(row)
                row.pop('error',None)
                try:
                    if not row.get('inventory'):
                        j['phase']='正在导出：'+s['name'];self.save_job(j)
                        scope={t:s['name']}
                        voice_mode=self.config.get('voice_mode','local')
                        if self.client_factory is Client and voice_mode=='local':
                            from .local_voice import status
                            found=status(self.config)
                            if not found['model_found'] or not found['runtime_ready']:raise ValueError('本地模型或转写组件缺失，请查看语音设置；也可选择仅复用已有转写')
                        if self.client_factory is Client and (voice_mode=='local' or (voice_mode=='api' and self.config.get('stt_url') and self.config.get('stt_model'))):
                            client=VoiceClient(j['weflow_config'],scope,self.config,self.stt_key,self.root/'transcripts')
                        else:client=self.client_factory(j['weflow_config'],scope=scope)
                        from .device_paths import update_state
                        export=Path(j['export_directory'])
                        report=Updater(client,update_state(self.root,export),export,export/'texts',scope={t:s['name']}).run(start=j['start'],end=j['end'],quiet=True)
                        result=report['conversations'][0];row.update(result)
                        if result.get('error'):raise ValueError(result['error'])
                    if remote:
                        archive=folder/(t+'.zip')
                        if not row.get('archive'):
                            package_inventory(row['inventory'],archive,j['export_directory']);row['archive']=str(archive);self.save_job(j)
                        oid=self.config['mapping'].get(t)
                        if not oid:
                            # Persistent identity in name makes a lost create response recoverable.
                            name=s['name'][:200-len(t)-3]+' ['+t+']'
                            objects=remote.call('/api/state')['objects'];found=[o for o in objects if o['name']==name]
                            if len(found)>1:raise ValueError('网站对象重复，请手动选择对应对象')
                            obj=found[0] if found else remote.call('/api/objects',{'name':name,'kind':s['kind']})
                            oid=obj['id'];self.config['mapping'][t]=oid;atomic_json(self.path,self.config)
                        j['phase']='正在上传：'+s['name'];self.save_job(j)
                        row['upload_result']=remote.call('/api/uploads?'+parse.urlencode({'object_id':oid,'filename':'records.zip','start':datetime.fromtimestamp(j['start'],TZ).isoformat(),'end':datetime.fromtimestamp(j['end'],TZ).isoformat()}),file=row['archive'])
                        row['object_id']=oid;row['uploaded']=True
                except (ValueError,RuntimeError,OSError) as exc:
                    # Never persist exception text from third-party libraries or credentials.
                    row['error']=str(exc)[:300] if isinstance(exc,(ValueError,RuntimeError)) else '文件读取失败，请核对路径和磁盘空间'
                    for secret in (self.password,self.stt_key):
                        if secret:row['error']=row['error'].replace(secret,'[已隐藏]')
                self.save_job(j)
            failures=any(r.get('error') for r in j['conversations'])
            if not failures:
                self.config['selection']=[s['external_id'] for s in j['selection']];atomic_json(self.path,self.config)
                if j['mode'] in ('api','agent') and not j.get('analysis_job') and not j.get('analysis_requested'):
                    j['phase']='正在创建网站分析任务';self.save_job(j)
                    # Lost response can mean analysis already started. Never recreate on retry.
                    j['analysis_requested']=True;self.save_job(j)
                    j['analysis_job']=remote.call('/api/pipeline/jobs',{'sessions':[r['object_id'] for r in j['conversations']], 'mode':j['mode']})['id']
            j['status']='partial' if failures else 'completed';j['phase']='部分失败，可重试未完成部分' if failures else '更新完成'
            if not failures and j.get('analysis_requested') and not j.get('analysis_job'):j['phase']='上传完成；分析请求结果未确认，请到网站核对，未重复创建'
        except (ValueError,RuntimeError,OSError,KeyError):
            j['status']='partial';j['phase']='未完成，请检查连接后重试；已导出和上传的部分保留'
        finally:
            with self.lock:
                self.running=False
                try:self.save_job(j)
                except OSError:
                    j.update(status='partial',phase='任务日志无法保存，请检查文件占用或磁盘权限后重试；已完成部分保留')
                    self._job_cache[j['id']]=copy.deepcopy(j)


def make_server(assistant,port=8771):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,data,mime='application/json; charset=utf-8'):
            body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(body)
        def allowed(self):return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
        def do_GET(self):
            if not self.allowed():return self.send(403,{'error':'仅允许本机访问'})
            if self.path=='/api/device/state':return self.send(200,{'connected':True,**assistant.public()})
            if self.path=='/api/health':return self.send(200,{'assistant':True,'root':str(assistant.root.resolve())})
            files={'/device-settings':('assistant.html','text/html; charset=utf-8'),'/assistant.js':('assistant.js','text/javascript; charset=utf-8'),'/assistant.css':('assistant.css','text/css; charset=utf-8')}
            if self.path in files:
                name,mime=files[self.path];return self.send(200,(WEB/name).read_bytes(),mime)
            if self.path.startswith('/api/device/'):return self.send(404,{'error':'未找到本机操作'})
            from .gateway import proxy
            return proxy(self,assistant)
        def do_POST(self):
            origins=[f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}']
            if not self.allowed() or self.headers.get('Origin') not in origins or self.headers.get('Sec-Fetch-Site')=='cross-site':return self.send(403,{'error':'请从更新助手页面操作'})
            if self.headers.get('Content-Type')!='application/json':return self.send(415,{'error':'请求格式错误'})
            try:
                n=int(self.headers.get('Content-Length','0'))
                limit=128*1024 if self.path.startswith('/api/device/') else 8*1024**2
                if not 1<=n<=limit:raise ValueError('请求大小无效')
                self.connection.settimeout(20);p=json.loads(self.rfile.read(n))
                if not isinstance(p,dict):raise ValueError('请求格式错误')
                if not self.path.startswith('/api/device/'):
                    from .gateway import proxy
                    return proxy(self,assistant,json.dumps(p,ensure_ascii=False).encode())
                if self.path=='/api/device/quit':
                    if assistant.running:raise ValueError('更新正在进行，请完成后关闭助手')
                    self.send(200,{'closed':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
                methods={'/api/device/delete':assistant.delete_job,'/api/device/settings':assistant.save,'/api/device/contacts':lambda _:assistant.refresh(),'/api/device/connect':lambda _:assistant.connect(),'/api/device/preset':assistant.preset,'/api/device/start':assistant.start}
                if self.path not in methods:return self.send(404,{'error':'未找到操作'})
                self.send(200,methods[self.path](p))
            except (ValueError,RuntimeError) as exc:
                text=str(exc)[:300]
                for secret in (assistant.password,assistant.stt_key):
                    if secret:text=text.replace(secret,'[已隐藏]')
                self.send(400,{'error':text})
            except (OSError,KeyError):self.send(400,{'error':'操作未完成，请核对路径、连接和选择范围'})
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True;return server


def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--data',default=str(Path(__file__).resolve().parents[1]/'data/assistant'));p.add_argument('--port',type=int,default=8771);a=p.parse_args()
    server=make_server(Assistant(a.data),a.port)
    print(f'WCWD更新助手 http://127.0.0.1:{server.server_port}',flush=True)
    server.serve_forever()

if __name__=='__main__':main()
