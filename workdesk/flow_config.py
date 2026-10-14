"""Local analysis settings. Credentials never appear in public settings or jobs."""
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import uuid
from .store import DeskError

DEFAULT_GOAL = '''围绕我的专业主任工作，分析本次更新的聊天记录，结合已确认的个人与工作背景、历史沟通、现有事项和人工反馈，整理与我有关的新增或变化事项。
核对通知、转达、提交、落实、改期及撤销之间的关系；必要时回查更早的记录和相关附件。排除兼职和私人事务。每项结论提供可回查依据，明确责任、期限和进度上的不确定性。
沿用已有事项标识，保留我的人工状态、反馈和编号。发现背景变化时提出完整背景更新建议，等待确认。报告影响结论的未读材料和资料缺口。'''
DEFAULTS = dict(base_url='',model='',default_goal=DEFAULT_GOAL,context_chars=48000,
    chunk_chars=10000,max_tokens=8192,timeout=180,temperature=0.2,vision=False,
    json_mode=False,output_parameter='max_tokens',lookup_rounds=2,
    material_roots=[],last_selection=[],extra_parameters={})

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.parent/('.write-'+uuid.uuid4().hex)
    try:
        with temp.open('w',encoding='utf8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        # Windows readers and antivirus may briefly hold a replace-denying handle.
        # Retry only the atomic local rename; never replay exports or network calls.
        for attempt in range(7):
            try:
                os.replace(temp,path);break
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==6:raise
                time.sleep(.02*(2**attempt))
    finally:temp.unlink(missing_ok=True)

def protect(value, decrypt=False):
    if not value:return ''
    if os.name!='nt':
        from cryptography.fernet import Fernet, InvalidToken
        secret_path=os.environ.get('WCWD_SECRET_KEY_FILE')
        if not secret_path:raise DeskError('服务器尚未配置模型密钥加密文件')
        secret=Path(secret_path)
        if secret.stat().st_mode & 0o077:raise DeskError('服务器密钥文件权限须为600')
        cipher=Fernet(secret.read_bytes().strip())
        if decrypt:
            if not value.startswith('fernet:'):raise DeskError('此模型密钥来自其他设备，请重新保存')
            try:return cipher.decrypt(value[7:].encode()).decode('utf8')
            except InvalidToken:raise DeskError('模型密钥无法解密') from None
        return 'fernet:'+cipher.encrypt(value.encode('utf8')).decode('ascii')
    class Blob(C.Structure):_fields_=[('size',W.DWORD),('data',C.c_void_p)]
    data=base64.b64decode(value[6:]) if decrypt else value.encode('utf8')
    buf=C.create_string_buffer(data);src=Blob(len(data),C.cast(buf,C.c_void_p));out=Blob()
    lib=C.WinDLL('crypt32',use_last_error=True);kernel=C.WinDLL('kernel32');kernel.LocalFree.argtypes=[C.c_void_p]
    if decrypt:
        fn=lib.CryptUnprotectData;fn.argtypes=[C.POINTER(Blob),C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,W.DWORD,C.POINTER(Blob)]
    else:
        fn=lib.CryptProtectData;fn.argtypes=[C.POINTER(Blob),W.LPCWSTR,C.c_void_p,C.c_void_p,C.c_void_p,W.DWORD,C.POINTER(Blob)]
    if not fn(C.byref(src),None,None,None,None,1,C.byref(out)):raise DeskError('无法读取或保存密钥，请使用原 Windows 用户')
    try:
        result=C.string_at(out.data,out.size)
        return result.decode('utf8') if decrypt else 'dpapi:'+base64.b64encode(result).decode('ascii')
    finally:kernel.LocalFree(out.data)

from .model_profiles import Profiles

class Settings(Profiles):
    def __init__(self,root):
        self.path=Path(root)/'flow/settings.json';self.lock=threading.RLock()
    def internal(self):
        with self.lock:
            data=json.loads(self.path.read_text('utf8')) if self.path.exists() else {}
            return {**DEFAULTS,**data}
    def _public_current(self):
        c=self.internal();c['key_saved']=bool(c.pop('encrypted_key','') or os.environ.get('WCWD_API_KEY'))
        c['api_ready']=bool(c['base_url'] and c['model'])
        from .device_paths import load_paths
        c['device_paths']=load_paths(self)
        c['default_goal_template']=DEFAULT_GOAL
        return c
    def key(self):
        value=self.internal().get('encrypted_key','')
        return protect(value,True) if value else os.environ.get('WCWD_API_KEY','')
    def _save_current(self,payload):
        with self.lock:
            c=self.internal()
            for k in DEFAULTS:
                if k in payload:c[k]=payload[k]
            for k in ('base_url','model','default_goal'):
                if not isinstance(c[k],str) or len(c[k])>(6000 if k=='default_goal' else 2000):raise DeskError('设置文本格式或长度不正确')
                c[k]=c[k].strip()
            if not c['default_goal']:raise DeskError('默认分析需求不能为空')
            if c['base_url']:
                u=urlsplit(c['base_url'])
                if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                    raise DeskError('API 地址须为 http/https 基础地址，不含密钥或查询参数')
                if u.scheme=='http' and u.hostname not in ('127.0.0.1','localhost','::1'):
                    raise DeskError('远程 API 请使用 https')
                c['base_url']=c['base_url'].rstrip('/')
            bounds={'context_chars':(16000,300000),'chunk_chars':(2000,30000),'max_tokens':(512,65536),'timeout':(10,600),'lookup_rounds':(0,3)}
            for k,(lo,hi) in bounds.items():
                if type(c[k]) is not int or not lo<=c[k]<=hi:raise DeskError(f'{k} 超出允许范围')
            if c['context_chars']<c['chunk_chars']+8000:raise DeskError('输入材料上限须至少比每段长度多8000字符')
            if type(c['temperature']) not in (int,float) or not 0<=c['temperature']<=2:raise DeskError('temperature 须在0到2之间')
            for k in ('vision','json_mode'):
                if type(c[k]) is not bool:raise DeskError('模型能力设置须为布尔值')
            if c['output_parameter'] not in ('max_tokens','max_completion_tokens'):raise DeskError('输出长度参数无效')
            if not isinstance(c['material_roots'],list) or len(c['material_roots'])>12 or any(not isinstance(p,str) or len(p)>1000 for p in c['material_roots']):raise DeskError('背景资料最多12个文件或目录')
            if not isinstance(c['last_selection'],list) or len(c['last_selection'])>50 or any(not isinstance(t,str) for t in c['last_selection']):raise DeskError('会话选择格式无效')
            if not isinstance(c['extra_parameters'],dict) or len(json.dumps(c['extra_parameters']))>4000:raise DeskError('额外模型参数须为小型 JSON 对象')
            if set(c['extra_parameters']) & {'model','messages','tools','stream','response_format','api_key','Authorization'}:raise DeskError('额外参数不能覆盖材料、模型或认证')
            if payload.get('clear_key'):c.pop('encrypted_key',None)
            if payload.get('api_key'):
                if not isinstance(payload['api_key'],str) or len(payload['api_key'])>4000:raise DeskError('密钥格式无效')
                c['encrypted_key']=protect(payload['api_key'])
            atomic_json(self.path,c)
            return self._public_current()
