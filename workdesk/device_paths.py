"""Paths belong to this device, independent of the selected model profile."""
import json, os
import hashlib, sqlite3
from pathlib import Path
from .store import DeskError

def load_paths(settings):
    path=settings.path.parent/'device-paths.json'
    if path.exists():return json.loads(path.read_text('utf8'))
    legacy=Path('E:/wechatrecord')
    export=legacy if legacy.is_dir() else Path.home()/'Documents'/'WeChatWorkDesk'/'wechatrecord'
    candidates=[Path('D:/weflow/WeFlow.exe'),Path(os.environ.get('ProgramFiles','C:/Program Files'))/'WeFlow'/'WeFlow.exe',Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Programs'/'WeFlow'/'WeFlow.exe']
    exe=next((str(p) for p in candidates if p.is_file()),'')
    config=Path(os.environ.get('APPDATA',str(Path.home())))/'weflow'/'WeFlow-config.json'
    return {'export_directory':str(export),'weflow_executable':exe,'weflow_config':str(config)}

def save_paths(settings,payload):
    from .flow_config import atomic_json
    if not isinstance(payload,dict):raise DeskError('设备路径设置格式错误')
    c=load_paths(settings)
    for k in c:
        if k not in payload:continue
        v=payload[k]
        if not isinstance(v,str) or len(v)>2000:raise DeskError('路径须为文本')
        v=v.strip()
        if not v and k=='weflow_executable':c[k]='';continue
        p=Path(v)
        if not v or not p.is_absolute():raise DeskError('请填写完整的本机绝对路径')
        if not Path(p.anchor).exists():raise DeskError('路径所在磁盘不存在，请选择本机目录')
        if k=='export_directory':
            if p.exists() and not p.is_dir():raise DeskError('聊天记录路径必须是目录')
            root=settings.path.parents[1].resolve()
            resolved=p.resolve()
            if resolved==root or resolved.is_relative_to(root) or root.is_relative_to(resolved):raise DeskError('聊天记录目录不能包含工作台数据目录或位于其中')
        elif not p.is_file():raise DeskError('WeFlow程序或配置文件不存在，请核对路径')
        c[k]=str(p)
    atomic_json(settings.path.parent/'device-paths.json',c)
    return c

def update_state(root, export):
    """Keep dedup ledgers scoped to their output directory; reuse the legacy ledger."""
    base=Path(root)/'chat-update';target=Path(export).resolve()
    old=base/'update.sqlite3'
    if old.exists():
        with sqlite3.connect(old) as con:
            row=con.execute("SELECT old_source FROM messages WHERE old_source!='' LIMIT 1").fetchone()
        if row:
            source=Path(row[0])
            for parent in source.parents:
                if parent.name in ('texts','increments') and parent.parent.resolve()==target:return base
    identity=hashlib.sha256(str(target).casefold().encode()).hexdigest()[:16]
    return base/'directories'/identity
