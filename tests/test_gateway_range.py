import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from workdesk.analysis_range import freeze_window,bounds
from workdesk.cloud import CloudPipeline
from workdesk.store import Store,DeskError
from workdesk.assistant import Assistant
from workdesk.gateway import proxy


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data');self.obj=self.store.add_object('模拟群');self.flow=CloudPipeline(self.store)
        self.md=self.root/'messages.md'
        self.md.write_text('# 群\n## 2026-10-01T10:00:00+08:00 甲\n消息标识: server:1\n范围外通知\n## 2026-10-02T12:00:00+08:00 乙\n消息标识: server:2\n需要处理的消息\n','utf8')
        self.flow.upload(self.md,'chat.md',self.obj['id'])
    def scope(self):return {'range':'custom','start':'2026-10-02','end':'2026-10-02'}
    def test_frozen_window_excludes_outside_messages_and_keeps_original_lookup(self):
        j=self.flow.create({'sessions':[self.obj['id']],'mode':'agent',**self.scope()},False)
        ctx=self.flow.context(j['id']);sid=ctx['pipeline']['primary_sources'][0]
        text=self.store.source(sid)[1].read_text('utf8')
        self.assertIn('需要处理的消息',text);self.assertNotIn('范围外通知',text)
        self.assertEqual(ctx['pipeline']['summary'][0]['analysis_messages'],1)
        self.assertGreater(len(ctx['sources']),len(ctx['pipeline']['primary_sources']))
        self.assertEqual(self.flow.analysis_scope()['sessions'],[self.obj['id']])
    def test_overlapping_snapshots_latest_message_wins(self):
        self.md.write_text(self.md.read_text('utf8').replace('需要处理的消息','后来补出的转写'),'utf8')
        self.flow.upload(self.md,'chat.md',self.obj['id'])
        primary,_,summary,_=freeze_window(self.store,self.obj['id'],*bounds(self.scope()))
        text=self.store.source(primary[0])[1].read_text('utf8')
        self.assertIn('后来补出的转写',text);self.assertNotIn('需要处理的消息',text);self.assertEqual(summary['analysis_messages'],1)
    def test_empty_window_does_not_create_reading_run(self):
        with self.assertRaises(DeskError):self.flow.create({'sessions':[self.obj['id']],'mode':'agent','range':'custom','start':'2026-10-03','end':'2026-10-03'},False)
        self.assertFalse(self.store.dashboard()['runs'])
    def test_unknown_date_is_explicitly_unverified(self):
        self.md.write_text('未含时间的旧记录','utf8');self.flow.upload(self.md,'chat.md',self.obj['id'])
        j=self.flow.create({'sessions':[self.obj['id']],'mode':'agent',**self.scope()},False)
        self.assertTrue(j['warnings'])
    def test_latest_upload_ignores_derived_analysis_import(self):
        self.flow.create({'sessions':[self.obj['id']],'mode':'agent',**self.scope()},False)
        j=self.flow.create({'sessions':[self.obj['id']],'mode':'agent'},False)
        ctx=self.flow.context(j['id']);sid=ctx['pipeline']['primary_sources'][0]
        self.assertNotIn('analysis/',self.store.source(sid)[0]['relative_path'])
    def test_bad_dates_rejected(self):
        for p in [{'range':'invalid'},{'range':'custom','start':'2026-10-02','end':'2026-10-01'},{'range':'custom','start':'text','end':'text'}]:
            with self.assertRaises(DeskError):bounds(p)
    def test_markdown_heading_inside_message_does_not_truncate_body(self):
        self.md.write_text(self.md.read_text('utf8').replace('需要处理的消息','需要处理的消息\n## 补充说明\n正文末尾'),'utf8')
        self.flow.upload(self.md,'chat.md',self.obj['id'])
        primary,_,_,_=freeze_window(self.store,self.obj['id'],*bounds(self.scope()))
        self.assertIn('正文末尾',self.store.source(primary[0])[1].read_text('utf8'))
    def test_background_files_are_context_not_new_message_primary(self):
        folder=self.root/'background';folder.mkdir();bg=folder/'profile.md';bg.write_text('专业主任工作背景。','utf8')
        self.store.register_materials(self.obj['id'],[('background/profile.md',bg)])
        j=self.flow.create({'sessions':[self.obj['id']],'mode':'agent',**self.scope()},False)
        ctx=self.flow.context(j['id'])
        self.assertEqual(len(ctx['pipeline']['primary_sources']),1)
        self.assertEqual(len(ctx['pipeline']['background_sources']),1)
        self.assertFalse(j['warnings'])


class GatewayTests(unittest.TestCase):
    def test_local_request_uses_website_auth_and_origin_and_preserves_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=Assistant(tmp);a.password='simulation-only'
            class Handler:
                path='/api/matters/abc/feedback';command='POST';headers={'Content-Type':'application/json'};wfile=io.BytesIO()
                def send_response(self,n):self.code=n
                def send_header(self,k,v):pass
                def end_headers(self):pass
            h=Handler()
            class Response:
                status=200;headers={'Content-Type':'application/json'}
                def __enter__(self):return self
                def __exit__(self,*args):pass
                stream=io.BytesIO(b'{"saved":true}')
                def read(self,n):return self.stream.read(n)
            class Opener:
                def open(self,r,timeout):
                    self.request=r;return Response()
            opener=Opener()
            with patch('workdesk.gateway.request.build_opener',return_value=opener):proxy(h,a,b'{}')
            self.assertEqual(opener.request.full_url,'https://desk.plantisland.cn'+h.path)
            self.assertEqual(opener.request.get_header('Origin'),'https://desk.plantisland.cn')
            self.assertTrue(opener.request.get_header('Authorization').startswith('Basic '))
            self.assertEqual(h.code,200);self.assertEqual(json.loads(h.wfile.getvalue()),{'saved':True})
    def test_proxy_cannot_target_another_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Handler:
                path='//evil.example/api/state'
                def send(self,n,value):self.code=n
            h=Handler();proxy(h,Assistant(tmp));self.assertEqual(h.code,400)

if __name__=='__main__':unittest.main()
