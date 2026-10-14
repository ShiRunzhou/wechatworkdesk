import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from workdesk.chat_update import Updater, TZ, identity, Baseline

class Fake:
    def __init__(self, rows):
        self.rows = rows; self.calls = []; self.fail_offset = None; self.empty_more = False
    def names(self, talker):
        return {talker: {'测试'}}
    def page(self, talker, start, end, offset, limit):
        self.calls.append((talker,start,end,offset,limit))
        if offset == self.fail_offset:
            raise RuntimeError('模拟分页失败')
        rows = self.rows[talker][offset:offset+limit]
        return dict(talker=talker,messages=rows,hasMore=self.empty_more or offset+limit<len(self.rows[talker]))
    def media(self, url):
        return b'fake png'

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.texts=self.root/'texts'; self.texts.mkdir()
        self.now=datetime(2026,10,13,12,30,0,tzinfo=TZ); self.end=int(self.now.timestamp())
    def msg(self, sid, delta=0, **kw):
        return dict(serverId=str(sid),localId=sid,createTime=self.end+delta,localType=1,
                    content='测试内容'+str(sid),senderUsername='a',isSend=0,sortSeq=sid,**kw)
    def updater(self, fake, scope=None):
        return Updater(fake,self.root/'state',self.root/'export',self.texts,scope or {'a':'测试'},limit=2)
    def test_range_paging_dedup_old_protection(self):
        old=self.texts/'old.md'; old.write_text('# 保留原文件\n','utf8'); original=old.read_bytes()
        fake=Fake({'a':[self.msg(1,-604801),self.msg(2,-604800),self.msg(3),self.msg(4,1)]})
        u=self.updater(fake); r=u.run(self.now)['conversations'][0]
        self.assertEqual((r['added'],r['outside']), (2,2))
        self.assertEqual(fake.calls[0][1:3],(self.end-604800,self.end))
        self.assertEqual([x[3] for x in fake.calls],[0,2])
        before={p:p.read_bytes() for p in (self.root/'export').rglob('*') if p.is_file()}
        r=u.run(self.now)['conversations'][0]
        self.assertEqual((r['added'],r['skipped']), (0,2))
        self.assertEqual(old.read_bytes(),original)
        self.assertTrue(all(p.read_bytes()==b for p,b in before.items()))
    def test_failure_and_resume(self):
        fake=Fake({'a':[self.msg(1),self.msg(2),self.msg(3)],'b':[self.msg(4)]}); fake.fail_offset=2
        u=self.updater(fake,{'a':'A','b':'B'}); report=u.run(self.now)
        self.assertTrue(report['conversations'][0]['error'])
        self.assertEqual(report['conversations'][1]['added'],1)
        fake.fail_offset=None; r=u.run(self.now,retry={'a'})['conversations'][0]
        self.assertEqual((r['added'],r['skipped']), (1,2))
    def test_crash_recovery(self):
        fake=Fake({'a':[self.msg(1)]}); u=self.updater(fake);u.run(self.now)
        import sqlite3
        with sqlite3.connect(self.root/'state/update.sqlite3') as db:
            db.execute('DELETE FROM messages')
        db.close()
        self.assertEqual(u.run(self.now)['conversations'][0]['added'],0)
        self.assertEqual(len(list((self.root/'export').rglob('*.md'))),1)
    def test_baseline_exact_message_not_entire_session(self):
        stamp=self.now.strftime('%Y-%m-%d %H:%M:%S').replace('-',r'\-')
        old=self.texts/'old.md';old.write_text(f'# 测试\n- 会话ID: `a`\n\n## {stamp} 测试\n\n测试内容1\n','utf8')
        fake=Fake({'a':[self.msg(1),self.msg(2)]});r=self.updater(fake).run(self.now)['conversations'][0]
        self.assertEqual((r['added'],r['skipped']), (1,1))
    def test_voice_reuse_and_gap_no_audio(self):
        stamp=self.now.strftime('%Y-%m-%d %H:%M:%S')
        (self.texts/'old.md').write_text(f'- 会话ID: `a`\n\n## {stamp} 测试\n\n[语音文件](media/voices/voice_a_1_{self.end}_1.wav)\n\n\\[语音转文字\\] 已有转写\n','utf8')
        one=self.msg(1);one['localType']=34
        two=self.msg(2);two['localType']=34
        r=self.updater(Fake({'a':[one,two]})).run(self.now)['conversations'][0]
        self.assertEqual((r['added'],r['skipped'],r['transcription_gaps']),(1,1,1))
        self.assertFalse(list((self.root/'export').rglob('*.wav')))
    def test_images_repeated_run_keeps_bytes(self):
        m=self.msg(1);m.update(localType=3,mediaUrl='http://127.0.0.1:5031/api/v1/media/x.png',mediaFileName='x.png')
        u=self.updater(Fake({'a':[m]}));u.run(self.now)
        p=next((self.root/'export').rglob('*.png')); before=p.read_bytes();u.run(self.now)
        self.assertEqual(p.read_bytes(),before)
    def test_local_collision_and_missing_id(self):
        m=self.msg(0);m['localId']=1;n=dict(m,createTime=m['createTime']-1)
        self.assertNotEqual(identity(m),identity(n))
        with self.assertRaises(ValueError):identity(dict(serverId='0'))
    def test_empty_more_is_failure(self):
        fake=Fake({'a':[]});fake.empty_more=True
        self.assertTrue(self.updater(fake).run(self.now)['conversations'][0]['error'])

    def test_cached_transcript_backfills_without_rewriting(self):
        m=self.msg(1);m['localType']=34
        fake=Fake({'a':[m]});fake.transcript=lambda talker,msg:''
        u=self.updater(fake);r=u.run(self.now)['conversations'][0]
        self.assertEqual(r['transcription_gaps'],1)
        old=next((self.root/'export').rglob('*.md'));oldbytes=old.read_bytes()
        fake.transcript=lambda talker,msg:'本机模型缓存的文字'
        r=u.run(self.now)['conversations'][0]
        self.assertEqual(r['transcription_gaps'],0)
        self.assertEqual(old.read_bytes(),oldbytes)
        self.assertEqual(len(list((self.root/'export').rglob('*-transcript.md'))),1)
        u.run(self.now)
        self.assertEqual(len(list((self.root/'export').rglob('*-transcript.md'))),1)

    def test_exclusive_publish_never_replaces_existing(self):
        from workdesk.chat_update import write_new
        p=self.root/'keep.md';write_new(p,b'original')
        with self.assertRaises(FileExistsError):write_new(p,b'replacement')
        self.assertEqual(p.read_bytes(),b'original')
        self.assertFalse(list(self.root.glob('.pending-*')))

    def test_file_format_existing_attachment_and_missing_file(self):
        stamp=self.now.strftime('%Y-%m-%d %H:%M:%S')
        (self.texts/'file.pdf').write_bytes(b'original attachment')
        (self.texts/'old.md').write_text(f'- 会话ID: `a`\n\n## {stamp} 测试\n\n[方案](file.pdf)\n\n\\[文件\\]方案.pdf\n','utf8')
        m=self.msg(1);m.update(content='[文件] 方案.pdf',localType=49)
        n=self.msg(2);n.update(content='[文件] 缺失.pdf',localType=49)
        u=self.updater(Fake({'a':[m,n]}));r=u.run(self.now)['conversations'][0]
        self.assertEqual((r['added'],r['skipped'],r['attachment_gaps']),(1,1,1))
        r=u.run(self.now)['conversations'][0]
        self.assertEqual(r['attachment_gaps'],1)

    def test_group_self_and_reply_old_format_match(self):
        stamp=self.now.strftime('%Y-%m-%d %H:%M:%S')
        (self.texts/'old.md').write_text(f'- 会话ID: `a`\n\n## {stamp} 自己的名字\n\n> 对方: 引用文字\n\n回复正文\n','utf8')
        m=self.msg(1);m.update(content='回复正文',localType=244813135921,isSend=1,senderUsername='self')
        fake=Fake({'a':[m]});fake.names=lambda talker:{'self':{'自己的名字'}}
        r=self.updater(fake).run(self.now)['conversations'][0]
        self.assertEqual((r['added'],r['skipped']),(0,1))

if __name__=='__main__': unittest.main()
