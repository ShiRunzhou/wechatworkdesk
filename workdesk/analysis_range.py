"""Focus analysis on message time, while keeping originals available for lookup."""
import hashlib
import json
import posixpath
import re
import tempfile
from datetime import datetime,timedelta
from pathlib import Path
from urllib.parse import unquote
from .chat_update import TZ
from .store import DeskError


def bounds(payload):
    preset=payload.get('range','latest');end=datetime.now(TZ)
    if preset=='latest':return None
    if preset in ('7d','30d'):start=end-timedelta(days=7 if preset=='7d' else 30)
    elif preset=='custom':
        try:
            def date(raw,is_end=False):
                if len(raw)==10:
                    value=datetime.strptime(raw.replace('/','-'),'%Y-%m-%d').replace(tzinfo=TZ)
                    if value.date()>end.date():raise ValueError()
                    return min(value+timedelta(days=1,microseconds=-1),end) if is_end else value
                value=datetime.fromisoformat(raw)
                if not value.tzinfo:raise ValueError()
                return value.astimezone(TZ)
            start=date(payload['start']);end=date(payload['end'],True)
        except (ValueError,TypeError,KeyError):raise DeskError('请提供有效的分析起止日期') from None
    else:raise DeskError('分析时间范围无效')
    if start>=end or end>datetime.now(TZ) or end-start>timedelta(days=366):raise DeskError('分析范围须按先后顺序，不能晚于现在或超过366天')
    return start,end


def freeze_window(store,oid,start,end):
    with store.connect() as con:
        rows=[dict(r) for r in con.execute("SELECT * FROM sources WHERE object_id=? AND cleaned_at='' ORDER BY created_at DESC,rowid DESC",(oid,))]
    blocks=[];seen=set();assets=set();unknown=[]
    for source in rows:
        if source['relative_path'].startswith('analysis/') or not source['relative_path'].lower().endswith('.md'):continue
        file=Path(source['snapshot_path'])
        if not file.is_file():continue
        text=file.read_text('utf-8-sig');headers=[m for m in re.finditer(r'^##\s+(.+)$',text,re.M) if re.match(r'\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}',m[1].replace('\\-','-'))];dated=False
        for i,heading in enumerate(headers):
            normalized=heading[1].replace('\\-','-')
            stamp=re.match(r'(\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)',normalized)
            if not stamp:continue
            try:
                when=datetime.fromisoformat(stamp[1].replace('/','-'))
                if not when.tzinfo:when=when.replace(tzinfo=TZ)
            except ValueError:continue
            dated=True
            if not start<=when<=end:continue
            block=text[heading.start():headers[i+1].start() if i+1<len(headers) else len(text)].strip()
            identity=re.search(r'(?:消息标识:\s*|message-key:\s*)([^\s`<>]+)',block)
            key=identity[1] if identity else hashlib.sha256(block.encode()).hexdigest()
            if key in seen:continue
            seen.add(key);blocks.append((when.timestamp(),block+'\n\n原消息来源ID: '+source['id']))
            for rel in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)',block):
                assets.add(posixpath.normpath(posixpath.join(posixpath.dirname(source['relative_path']),unquote(rel))))
        if not dated and text.strip() and '本轮所选时间没有消息' not in text:unknown.append(source['id'])
    asset_ids=[];picked=set()
    for row in rows:
        rel=row['relative_path']
        if rel in assets and rel not in picked and not rel.lower().endswith('.md'):
            asset_ids.append(row['id']);picked.add(rel)
    primary=[]
    if blocks:
        with tempfile.TemporaryDirectory(prefix='wcwd-analysis-range-') as tmp:
            file=Path(tmp)/'analysis/selected-messages.md';file.parent.mkdir()
            file.write_text('# 本轮分析重点资料\n\n消息范围：'+start.isoformat()+' 至 '+end.isoformat()+'\n原始资料仍可按来源ID回查。\n\n'+'\n\n'.join(b for _,b in sorted(blocks,key=lambda x:x[0])),'utf8')
            result=store.register_import(oid,Path(tmp),start.isoformat(),end.isoformat())
            with store.connect() as con:primary=[r[0] for r in con.execute('SELECT source_id FROM import_sources WHERE import_id=?',(result['id'],))]
    warnings=[]
    if unknown:
        primary.extend(unknown);warnings.append('部分历史Markdown无法识别消息日期，已保留供核查；不能据此断言覆盖所选时段。')
    if not primary:raise DeskError('所选对象在分析时段内没有可用聊天记录，请先更新该时段或调整范围')
    return primary,asset_ids,{'object_id':oid,'analysis_messages':len(blocks)},warnings
