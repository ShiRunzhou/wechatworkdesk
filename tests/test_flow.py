import copy
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib import request,error
from workdesk.store import Store,DeskError
from workdesk.flow import Pipeline
from workdesk.flow_config import Settings,protect
from workdesk.materials import Pool
from workdesk.model_api import ModelAPI,parse_result
from workdesk.server import make_server
from workdesk.chat_update import TZ

class FakeClient:
    account='simulation'
    def __init__(self):
        ts=int((datetime.now(TZ)-timedelta(hours=1)).timestamp())
        self.rows={'fake':[dict(localId=1,serverId='123456789',sortSeq=1,createTime=ts,localType=1,isSend=0,senderUsername='fake',content='请核对实习材料，10月20日前提交。')]}
        self.fail=False;self.pages=[]
    def get(self,path,params=None):return {'sessions':[{'username':'fake','displayName':'模拟专业组'}]}
    def names(self,t):return {t:{'模拟专业组'}}
    def transcript(self,t,m):return ''
    def page(self,t,start,end,offset,limit):
        self.pages.append((t,start,end,offset))
        if self.fail:raise RuntimeError('模拟会话读取失败')
        rows=[m for m in self.rows[t] if start<=m['createTime']<=end]
        return {'talker':t,'messages':rows[offset:offset+limit],'hasMore':offset+limit<len(rows)}

class FakeModel:
    def __init__(self):self.calls=[];self.fail_after=None;self.unsafe=False;self.queries=False
    def test(self):return {'connected':True,'models':['mock-model']}
    def complete(self,messages):
        self.calls.append(messages)
        if self.fail_after and len(self.calls)>=self.fail_after:raise DeskError('模拟模型中断')
        content=messages[-1]['content'];content=content if isinstance(content,str) else content[0]['text']
        data=json.loads(content);oid=data['context']['object_id']
        if data['draft_to_verify']:matters=copy.deepcopy(data['draft_to_verify'])
        else:
            sid=re.search(r'source_id=([a-f0-9]{32})',data['materials'][0])[1]
            locator=re.search(r'第\d+至\d+行',data['materials'][0])[0]
            matters=[dict(object_id=oid,dedup_key='intern-materials-2026',title='核对实习材料',detail='确认适用对象并准备实习材料。',
                notice_at='',deadline='10月20日前',owner='待核实',action='核实材料并提交',questions='是否由本人提交待确认',
                suggested_state='pending',status_unverified=True,evidence=[{'source_id':sid,'locator':locator}])]
        if self.unsafe:matters[0]['evidence'][0]['source_id']='f'*32
        return {'matters':matters,'backgrounds':[],'lookup_requests':['2026 实习'] if self.queries and not data['draft_to_verify'] else []},{'total_tokens':10}

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data');self.export=self.root/'export';(self.export/'texts').mkdir(parents=True)
        self.client=FakeClient();self.model=FakeModel()
        self.flow=Pipeline(self.store,self.export,lambda scope:self.client,lambda c,k:self.model)
        self.flow.sessions(True);self.flow.settings.save({'base_url':'http://127.0.0.1:1234/v1','model':'mock-model'})
    def run_job(self,mode='api',**extra):
        j=self.flow.create({'sessions':['fake'],'mode':mode,**extra},background=False);self.flow.run(j['id']);return self.flow.public_job(j['id'])
    def test_api_end_to_end_and_processed_reuse(self):
        j=self.run_job();self.assertEqual(j['status'],'completed',j.get('error'));self.assertEqual(j['results']['new'],1)
        m=self.store.dashboard()['matters'][0];self.assertEqual(m['state'],'unoperated');self.assertTrue(m['reference_code'])
        before=len(self.model.calls);next_job=self.run_job();self.assertEqual(next_job['status'],'completed',next_job.get('error'))
        self.assertEqual(next_job['material_summary'][0]['analysis_messages'],0);self.assertEqual(before,len(self.model.calls))
    def test_human_state_and_number_survive(self):
        j=self.run_job();m=self.store.dashboard()['matters'][0]
        self.store.feedback('matter',m['id'],dict(version=m['version'],state='completed',status_unverified=False,note='人工已完成'))
        j=self.run_job();self.assertEqual(j['status'],'completed',j.get('error'))
        n=self.store.dashboard()['matters'][0]
        self.assertEqual((n['id'],n['state'],n['human_note'],n['reference_code']),(m['id'],'completed','人工已完成',m['reference_code']))
    def test_model_failure_no_partial_publication_and_resume(self):
        self.model.fail_after=2;j=self.run_job();self.assertEqual(j['status'],'failed');self.assertFalse(self.store.dashboard()['matters'])
        self.model.fail_after=None;self.flow.retry(j['id'],background=False);self.flow.run(j['id']);j=self.flow.public_job(j['id'])
        self.assertEqual(j['status'],'completed',j.get('error'));self.assertEqual(len(self.model.calls),3)
        self.assertEqual(len(self.store.dashboard()['matters']),1)
    def test_increased_output_budget_retains_valid_checkpoint_attempt(self):
        self.model.fail_after=2;j=self.run_job();attempt=j['attempt'];done=j['analysis_done']
        self.flow.settings.save({'max_tokens':32768,'timeout':300})
        retry=self.flow.retry(j['id'],background=False)
        self.assertEqual(retry['attempt'],attempt);self.assertEqual(retry['analysis_done'],done)
        self.model.fail_after=None;self.flow.run(j['id'])
        self.assertEqual(self.flow.job(j['id'])['status'],'completed');self.assertEqual(len(self.model.calls),3)
    def test_changed_model_invalidates_previous_attempt(self):
        self.model.fail_after=2;j=self.run_job();attempt=j['attempt']
        self.flow.settings.save({'model':'different-model'})
        retry=self.flow.retry(j['id'],background=False)
        self.assertNotEqual(retry['attempt'],attempt);self.assertEqual(retry['analysis_done'],0)
    def test_deepseek_truncation_recovers_only_failed_call_and_keeps_global_mode(self):
        from workdesk.model_api import TruncatedOutput
        self.flow.settings.save({'base_url':'https://api.deepseek.com','extra_parameters':{'thinking':{'type':'enabled'},'reasoning_effort':'low'},'json_mode':True,'efficient_mode':False})
        model=FakeModel();configs=[];failure={'count':0}
        class Adapter:
            def __init__(self,c):self.c=c
            def complete(self,messages):
                configs.append(copy.deepcopy(self.c))
                if self.c['extra_parameters']['thinking']['type']=='enabled' and not failure['count']:
                    failure['count']+=1;raise TruncatedOutput({'message':{'content':''}},{'total_tokens':99,'completion_tokens_details':{'reasoning_tokens':80}})
                return model.complete(messages)
        self.flow.provider_factory=lambda c,k:Adapter(c)
        j=self.run_job();self.assertEqual(j['status'],'completed',j.get('error'))
        self.assertEqual(len(j['recovery_calls']),1);self.assertTrue(j['warnings'])
        self.assertEqual(configs[1]['extra_parameters']['thinking']['type'],'disabled')
        self.assertFalse(configs[1]['json_mode'])
        self.assertEqual(self.flow.settings.public()['extra_parameters']['thinking']['type'],'enabled')
        self.assertEqual(j['usage']['total_tokens'],119)
    def test_explicit_retry_of_truncated_step_does_not_repeat_failed_thinking(self):
        self.model.fail_after=2;j=self.run_job();raw=self.flow.job(j['id']);raw['error']='模型输出被截断';self.flow.save(raw)
        self.flow.settings.save({'base_url':'https://api.deepseek.com','extra_parameters':{'thinking':{'type':'enabled'}}})
        # Freeze these settings as if this was the failed request's original provider.
        raw=self.flow.job(j['id']);from workdesk.flow import config_snapshot
        raw['config']=config_snapshot(self.flow.settings);self.flow.save(raw)
        used=[];self.model.fail_after=None
        self.flow.provider_factory=lambda c,k:(used.append(copy.deepcopy(c)) or self.model)
        self.flow.retry(j['id'],background=False);self.flow.run(j['id'])
        self.assertEqual(self.flow.job(j['id'])['status'],'completed')
        self.assertEqual(used[-1]['extra_parameters']['thinking']['type'],'disabled')
        self.assertEqual(len(self.model.calls),3)
    def test_wrong_object_rejected_then_retry_regenerates_segment(self):
        original=self.model.complete
        def wrong(messages):
            result,usage=original(messages);result['matters'][0]['object_id']='f'*32
            result['matters'][0]['evidence'][0]['source_id']='f'*32;return result,usage
        self.model.complete=wrong;j=self.run_job()
        self.assertEqual(j['status'],'failed');self.assertIn('其他会话',j['error'])
        self.assertFalse(self.store.dashboard()['matters'])
        self.assertTrue(list(self.flow.folder(j['id']).glob('attempts/*/*.rejected-*.json')))
        self.model.complete=original;self.flow.retry(j['id'],background=False);self.flow.run(j['id'])
        self.assertEqual(self.flow.public_job(j['id'])['status'],'completed')
        self.assertEqual(len(self.model.calls),3)
    def test_legacy_invalid_checkpoint_regenerated_on_explicit_retry(self):
        self.model.fail_after=2;j=self.run_job()
        saved=next(self.flow.folder(j['id']).glob('attempts/*/*.json'))
        obj=json.loads(saved.read_text('utf8'));obj['result']['matters'][0]['object_id']='f'*32
        obj['result']['matters'][0]['evidence'][0]['source_id']='f'*32
        saved.write_text(json.dumps(obj),'utf8');self.model.fail_after=None
        self.flow.retry(j['id'],background=False);self.flow.run(j['id'])
        self.assertEqual(self.flow.public_job(j['id'])['status'],'completed')
        self.assertEqual(len(self.model.calls),4)
        self.assertTrue(list(saved.parent.glob('*.rejected-*.json')))
    def test_empty_stable_key_rejected(self):
        original=self.model.complete
        def wrong(messages):
            result,usage=original(messages);result['matters'][0]['dedup_key']=' ';return result,usage
        self.model.complete=wrong;j=self.run_job()
        self.assertEqual(j['status'],'failed');self.assertIn('dedup_key',j['error'])
        self.assertFalse(self.store.dashboard()['matters'])
    def test_program_binds_missing_or_mistyped_object_from_own_evidence(self):
        original=self.model.complete
        def typo(messages):
            result,usage=original(messages)
            if len(self.model.calls)==1:result['matters'][0]['object_id']='mistyped-internal-id'
            else:result['matters'][0].pop('object_id',None)
            return result,usage
        self.model.complete=typo;j=self.run_job()
        self.assertEqual(j['status'],'completed',j.get('error'))
        matter=self.store.dashboard()['matters'][0]
        self.assertEqual(matter['object_id'],self.flow.context(j['id'])['objects'][0]['id'])
        self.assertEqual(matter['dedup_key'],'intern-materials-2026')
    def test_known_foreign_object_not_remapped_even_with_own_evidence(self):
        j=self.run_job('agent');ctx=self.flow.context(j['id']);oid=ctx['objects'][0]['id']
        ctx['objects'].append({'id':'f'*32,'name':'另一个会话'})
        sid=ctx['pipeline']['primary_sources'][0]
        result={'matters':[{'object_id':'f'*32,'dedup_key':'foreign','evidence':[{'source_id':sid}]}]}
        with self.assertRaises(DeskError):self.flow._validate_segment(result,oid,ctx)
    def test_question_list_adapted_without_changing_meaning(self):
        original=self.model.complete
        def listing(messages):
            result,usage=original(messages)
            result['matters'][0]['questions']=['材料是否已提交？','负责人是否确认？']
            return result,usage
        self.model.complete=listing;j=self.run_job()
        self.assertEqual(j['status'],'completed',j.get('error'))
        self.assertEqual(self.store.dashboard()['matters'][0]['questions'],'材料是否已提交？\n负责人是否确认？')
    def test_update_failure_resume_keeps_range(self):
        self.client.fail=True;j=self.run_job('agent');self.assertEqual(j['status'],'failed')
        window=(j['start'],j['end']);self.client.fail=False;self.flow.retry(j['id'],background=False);self.flow.run(j['id'])
        j=self.flow.public_job(j['id']);self.assertEqual(j['status'],'waiting_agent');self.assertEqual((j['start'],j['end']),window)
    def test_agent_same_context_and_submission(self):
        j=self.run_job('agent');ctx=self.flow.context(j['id']);pool=Pool(ctx)
        sid=ctx['pipeline']['primary_sources'][0];self.assertIn('实习',self.flow.retrieve(j['id'],{'query':'实习'})['parts'][0]['text'])
        part=self.flow.retrieve(j['id'],{'source_id':sid},True)['parts'][0];self.assertTrue(part['text'])
        payload={'coverage':[{'source_id':s['id'],'state':'read','note':'已通过本轮快照完整读取模拟资料'} for s in ctx['sources']],
                 'matters':[],'backgrounds':[]}
        self.flow.submit(j['id'],payload);self.assertEqual(self.flow.public_job(j['id'])['status'],'completed')
    def test_unprovided_evidence_rejected(self):
        self.model.unsafe=True;j=self.run_job();self.assertEqual(j['status'],'failed');self.assertFalse(self.store.dashboard()['matters'])
    def test_evidence_line_out_of_provided_range_rejected(self):
        original=self.model.complete
        def bad(messages):
            result,usage=original(messages);result['matters'][0]['evidence'][0]['locator']='第99999至100000行';return result,usage
        self.model.complete=bad;j=self.run_job();self.assertEqual(j['status'],'failed');self.assertFalse(self.store.dashboard()['matters'])
    def test_background_retrieval_snapshot_and_scope(self):
        bg=self.root/'背景.md';bg.write_text('2026 实习：职责需要核对。','utf8');self.flow.settings.save({'material_roots':[str(bg)]})
        self.model.queries=True;j=self.run_job();self.assertEqual(j['status'],'completed',j.get('error'))
        ctx=self.flow.context(j['id']);pool=Pool(ctx);hits=pool.search('职责')
        self.assertTrue(hits);bg.write_text('原文已改','utf8');self.assertIn('职责',pool.search('职责')[0]['text'])
        with self.assertRaises(DeskError):pool.read('f'*32)
    def test_range_selection_and_path_validation(self):
        with self.assertRaises(DeskError):self.flow.create({'sessions':['../secret'],'mode':'update'},False)
        now=datetime.now(TZ);j=self.run_job('update',range='custom',start=(now-timedelta(days=2)).isoformat(),end=(now-timedelta(seconds=1)).isoformat())
        self.assertEqual(j['status'],'completed');self.assertEqual(j['conversations'][0]['added'],1)
        with self.assertRaises(DeskError):self.flow.create({'sessions':['fake'],'range':'custom','start':'2026-10-12','end':'2026-10-01'},False)
    def test_register_materials_wrong_owner_atomic(self):
        obj=self.store.add_object('A',external_id='a');other=self.store.add_object('B',external_id='b');p=self.root/'a.txt';p.write_text('a')
        imp=self.store.register_materials(obj['id'],[('a.txt',p)])
        with self.assertRaises(DeskError):self.store.register_materials(other['id'],[('b.txt',p)],prior_ids=[imp['sources'][0]['id']])
        with self.store.connect() as con:self.assertEqual(con.execute('select count(*) from imports where object_id=?',(other['id'],)).fetchone()[0],0)
    def test_config_key_encrypted_and_public_hidden(self):
        if os.name!='nt':self.skipTest('Windows DPAPI')
        self.flow.settings.save({'api_key':'test-private-token'})
        self.assertNotIn('test-private-token',self.flow.settings.path.read_text('utf8'))
        self.assertEqual(self.flow.settings.key(),'test-private-token');self.assertTrue(self.flow.settings.public()['key_saved'])
        j=self.run_job('agent');self.assertNotIn('test-private-token',json.dumps(j));self.assertNotIn('encrypted_key',self.flow.context(j['id']))
    def test_repeated_window_only_latest_current_source_primary(self):
        a=self.run_job('agent');self.client.rows['fake'][0]['content']+='补充'
        b=self.run_job('agent');ctx=self.flow.context(b['id'])
        self.assertEqual(len(ctx['pipeline']['primary_sources']),1)
        self.assertGreater(len(ctx['sources']),1)
    def test_cancellation_no_submission(self):
        original=self.model.complete
        def cancel(messages):
            self.flow.cancel_events[j['id']].set();return original(messages)
        j=self.flow.create({'sessions':['fake'],'mode':'api'},False);self.flow.cancel_events[j['id']]=threading.Event()
        self.model.complete=cancel;self.flow.run(j['id']);self.assertEqual(self.flow.public_job(j['id'])['status'],'cancelled')
        self.assertFalse(self.store.dashboard()['matters'])
    def test_changed_feedback_during_analysis_requires_recheck(self):
        self.run_job();m=self.store.dashboard()['matters'][0];original=self.model.complete;changed=[]
        def update_feedback(messages):
            result,usage=original(messages)
            if not changed:
                current=self.store.dashboard()['matters'][0]
                self.store.feedback('matter',current['id'],dict(version=current['version'],state='completed',status_unverified=False,note='分析期间人工核实完成'))
                changed.append(True)
            return result,usage
        self.model.complete=update_feedback;j=self.run_job(force_analysis=True)
        self.assertEqual(j['status'],'failed');self.assertIn('分析期间',j['error'])
        self.model.complete=original;self.flow.retry(j['id'],False);self.flow.run(j['id'])
        self.assertEqual(self.flow.public_job(j['id'])['status'],'completed')
        current=self.store.dashboard()['matters'][0];self.assertEqual(current['state'],'completed');self.assertEqual(current['reference_code'],m['reference_code'])
    def test_publication_checkpoint_recovery_does_not_call_model(self):
        original=self.flow._mark_processed
        def fail(j):raise DeskError('模拟发布后的进度保存失败')
        self.flow._mark_processed=fail;j=self.run_job();self.assertEqual(j['status'],'failed')
        self.assertEqual(len(self.store.dashboard()['matters']),1);calls=len(self.model.calls)
        self.flow._mark_processed=original;self.flow.retry(j['id'],False);self.flow.run(j['id'])
        self.assertEqual(self.flow.public_job(j['id'])['status'],'completed');self.assertEqual(len(self.model.calls),calls)

class HTTPFlowTests(unittest.TestCase):
    setUp=FlowTests.setUp
    def test_http_endpoints_and_origin_checks(self):
        server=make_server(self.store,0,self.flow);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown);base='http://127.0.0.1:'+str(server.server_port)
        with request.urlopen(base+'/update') as r:self.assertIn('更新分析模块'.encode(),r.read())
        with request.urlopen(base+'/api/pipeline/config') as r:self.assertTrue(json.load(r)['api_ready'])
        req=request.Request(base+'/api/pipeline/config',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://evil.example'})
        with self.assertRaises(error.HTTPError) as e:request.urlopen(req)
        self.assertEqual(e.exception.code,403)
        req=request.Request(base+'/api/pipeline/model-test',data=b'{}',headers={'Content-Type':'application/json'})
        with request.urlopen(req) as r:self.assertTrue(json.load(r)['connected'])

class AdapterTests(unittest.TestCase):
    def test_text_encoding_is_not_guessed_as_utf16_without_bom(self):
        from workdesk.materials import extract
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw)/'记录.csv';p.write_bytes('工作事项,待确认'.encode('gb18030'))
            self.assertEqual(extract(p)[0],'工作事项,待确认')
            p.write_bytes('工作事项,待确认'.encode('utf16'));self.assertEqual(extract(p)[0],'工作事项,待确认')
    def test_office_attachment_read_and_search_by_filename(self):
        import zipfile
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);p=root/'实习通知.docx'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>10月20日前提交工作手册。</w:t></w:r></w:p></w:body></w:document>')
            s=Store(root/'data');obj=s.add_object('模拟群');s.register_materials(obj['id'],[('实习通知.docx',p)])
            run=s.create_run([obj['id']],'模拟附件读取');pool=Pool(s.context(run['id']))
            hits=pool.search('实习通知');self.assertTrue(hits);self.assertIn('工作手册',hits[0]['text']);pool.mark(hits)
            self.assertEqual(pool.coverage()[0]['state'],'partial')
    def test_http_pipeline_through_real_adapter_without_tools(self):
        model=FakeModel()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'{"data":[{"id":"mock-model"}]}')
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                result,usage=model.complete(payload['messages'])
                out={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}],'usage':usage}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(out,ensure_ascii=False).encode('utf8'))
        ms=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=ms.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                root=Path(raw);store=Store(root/'data');export=root/'export';(export/'texts').mkdir(parents=True)
                flow=Pipeline(store,export,lambda scope:FakeClient());flow.sessions(True)
                ws=make_server(store,0,flow);threading.Thread(target=ws.serve_forever,daemon=True).start()
                try:
                    base='http://127.0.0.1:'+str(ws.server_port)
                    def post(path,payload):
                        req=request.Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                        with request.urlopen(req) as r:return json.load(r)
                    post('/api/pipeline/config',{'base_url':'http://127.0.0.1:'+str(ms.server_port)+'/v1','model':'mock-model'})
                    self.assertTrue(post('/api/pipeline/model-test',{})['connected'])
                    j=post('/api/pipeline/jobs',{'sessions':['fake'],'mode':'api'})
                    import time
                    for _ in range(100):
                        with request.urlopen(base+'/api/pipeline/jobs/'+j['id']) as r:j=json.load(r)
                        if j['status'] not in ('queued','running'):break
                        time.sleep(.03)
                    self.assertEqual(j['status'],'completed',j.get('error'));self.assertEqual(j['results']['new'],1)
                    self.assertEqual(store.dashboard()['matters'][0]['state'],'unoperated')
                finally:ws.shutdown();ws.server_close()
        finally:ms.shutdown();ms.server_close()

    def test_json_and_thinking_parsing(self):
        self.assertEqual(parse_result('<think>private</think>\n```json\n{"matters":[]}\n```'),{'matters':[]})
        with self.assertRaises(DeskError):parse_result('run some command')
    def test_actual_compatible_http_model_roundtrip(self):
        observed=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                b=b'{"data":[{"id":"mock"}]}';self.send_response(200);self.end_headers();self.wfile.write(b)
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])));observed.append(payload)
                b=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"matters":[],"backgrounds":[]}'}}]}).encode()
                self.send_response(200);self.end_headers();self.wfile.write(b)
        s=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=s.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as root:cfg=Settings(root).public()
            cfg.update(base_url='http://127.0.0.1:'+str(s.server_port)+'/v1',model='mock')
            client=ModelAPI(cfg);self.assertTrue(client.test()['connected']);result,_=client.complete([{'role':'user','content':'只分析模拟资料'}])
            self.assertEqual(result['matters'],[]);self.assertNotIn('tools',observed[0]);self.assertFalse(observed[0]['stream'])
        finally:s.shutdown();s.server_close()
