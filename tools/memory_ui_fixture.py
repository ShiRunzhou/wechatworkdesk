"""Serve synthetic work memory for UI review; never use the production data directory."""
import json
import sys
import tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workdesk.store import Store
from workdesk.server import make_server

with tempfile.TemporaryDirectory(prefix='wcwd-memory-ui-') as tmp:
    root=Path(tmp);file=root/'chat.md';file.write_text('模拟讲座报名通知。\n模拟实习事项已核对完成。','utf8')
    store=Store(root/'data');obj=store.add_object('模拟 · 文旅通知群')
    store.register_import(obj['id'],file);run=store.create_run([obj['id']],'模拟界面验证');sid=store.context(run['id'])['sources'][0]['id']
    items=[dict(object_id=obj['id'],dedup_key='simulated-'+str(i),title='讲座报名 · 模拟'+str(i),detail='仅为模拟通知，无真实聊天资料。',action='核对适用范围',notice_at='2026-01-01T00:00:00+08:00',evidence=[dict(source_id=sid,locator='第1行')]) for i in range(3)]
    store.submit(run['id'],dict(coverage=[dict(source_id=sid,state='read',note='模拟全文')],matters=items))
    for m in store.dashboard()['matters']:store.feedback('matter',m['id'],dict(version=m['version'],state='irrelevant',memory_category='讲座报名',note='模拟：本次无需本人参加'))
    m=store.dashboard()['matters'][0];store.feedback('matter',m['id'],dict(version=m['version'],state='completed',memory_note='模拟：已核对责任并反馈教学秘书，无需专业主任组织。'))
    store.save_memory(dict(object_id=obj['id'],content='模拟：面向全体教师的通知通常无需重复转达',kind='advice',match_terms=[],exceptions='明确要求专业主任转达的除外',state='active'))
    old=(datetime.now(timezone.utc)-timedelta(days=120)).isoformat()
    with store.connect() as con:con.execute('UPDATE sources SET last_seen_at=?,created_at=?',(old,old))
    server=make_server(store,8789)
    print('Synthetic UI ready: http://127.0.0.1:8789',flush=True)
    try:server.serve_forever()
    finally:server.server_close()
