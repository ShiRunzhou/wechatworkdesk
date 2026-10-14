import copy
import json
import tempfile
import unittest
from pathlib import Path
from workdesk.cloud import CloudPipeline
from workdesk.store import Store
from workdesk.analysis_range import bounds,freeze_window
from test_flow import FakeModel


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'data')
        self.model=FakeModel();self.configs=[]
        def factory(c,k):self.configs.append(copy.deepcopy(c));return self.model
        self.flow=CloudPipeline(self.store,provider_factory=factory)
        self.obj=self.store.add_object('测试专业群');self.md=self.root/'chat.md'
        self.md.write_text('# 群\n## 2026-10-02T12:00:00+08:00 甲\n消息标识: server:1\n10月20日前提交实习材料。\n','utf8')
        self.flow.upload(self.md,'chat.md',self.obj['id'])
        self.flow.settings.save({'base_url':'https://api.deepseek.com','model':'mock','max_tokens':32768,'extra_parameters':{'thinking':{'type':'enabled'},'reasoning_effort':'low'}})
        self.payload={'sessions':[self.obj['id']],'mode':'api','range':'custom','start':'2026-10-02','end':'2026-10-02'}

    def run_job(self,**extra):
        j=self.flow.create({**self.payload,**extra},False);self.flow.run(j['id']);return self.flow.job(j['id'])

    def test_overlap_reuses_published_messages_but_new_message_is_analyzed(self):
        first=self.run_job();self.assertEqual(first['status'],'completed',first['error'])
        count=len(self.model.calls);again=self.run_job()
        self.assertEqual(again['status'],'completed',again['error']);self.assertEqual(len(self.model.calls),count)
        self.assertEqual(self.flow.context(again['id'])['pipeline']['summary'][0]['reused_messages'],1)
        self.md.write_text(self.md.read_text('utf8')+'\n## 2026-10-02T13:00:00+08:00 乙\n消息标识: server:2\n实习材料已有补充。\n','utf8')
        self.flow.upload(self.md,'chat.md',self.obj['id']);last=self.run_job()
        self.assertEqual(last['status'],'completed',last['error'])
        summary=self.flow.context(last['id'])['pipeline']['summary'][0]
        self.assertEqual((summary['analysis_messages'],summary['reused_messages']),(1,1))
        material=json.loads(self.model.calls[count][-1]['content'])['materials'][0]
        self.assertIn('已有补充',material);self.assertNotIn('10月20日前提交',material)

    def test_changed_voice_transcript_and_force_analysis_do_not_skip(self):
        self.run_job();before=len(self.model.calls)
        self.md.write_text(self.md.read_text('utf8')+'[语音转文字] 已补签。\n','utf8');self.flow.upload(self.md,'chat.md',self.obj['id'])
        j=self.run_job();self.assertEqual(j['status'],'completed',j['error']);self.assertGreater(len(self.model.calls),before)
        before=len(self.model.calls);self.run_job(force_analysis=True);self.assertGreater(len(self.model.calls),before)

    def test_human_feedback_invalidates_cache_and_manual_state_survives(self):
        self.run_job();m=self.store.dashboard()['matters'][0];before=len(self.model.calls)
        self.store.feedback('matter',m['id'],{'version':m['version'],'state':'irrelevant','note':'本通知无需我转达'})
        j=self.run_job();self.assertEqual(j['status'],'completed',j['error']);self.assertGreater(len(self.model.calls),before)
        self.assertEqual(self.store.dashboard()['matters'][0]['state'],'irrelevant')

    def test_failed_or_partial_agent_task_does_not_mark_messages_processed(self):
        self.model.fail_after=2;j=self.run_job();self.assertEqual(j['status'],'failed')
        self.assertFalse((self.flow.root/'analysis-cache.json').exists())
        self.model.fail_after=None;j=self.flow.create({**self.payload,'mode':'agent'},False);self.flow.run(j['id'])
        ctx=self.flow.context(j['id']);result={'matters':[],'backgrounds':[],'coverage':[dict(source_id=s['id'],state='unread',note='尚未阅读') for s in ctx['sources']]}
        self.flow.submit(j['id'],result)
        fresh=self.flow.create(self.payload,False)
        self.assertEqual(self.flow.context(fresh['id'])['pipeline']['summary'][0]['analysis_messages'],1)

    def test_call_budget_pauses_then_resume_retains_paid_checkpoint(self):
        self.flow.settings.save({'max_model_calls':1});j=self.run_job()
        self.assertEqual(j['status'],'paused',j['error']);self.assertEqual(j['request_count'],1)
        self.assertFalse(self.store.dashboard()['matters']);attempt=j['attempt']
        self.flow.settings.save({'max_model_calls':4});self.flow.retry(j['id'],False);self.flow.run(j['id']);last=self.flow.job(j['id'])
        self.assertEqual(last['status'],'completed',last['error']);self.assertEqual(last['attempt'],attempt)
        self.assertEqual(len(self.model.calls),2)

    def test_token_budget_prevents_request_and_tracks_rejected_usage(self):
        self.flow.settings.save({'max_total_tokens':1000});j=self.run_job()
        self.assertEqual(j['status'],'paused');self.assertEqual(len(self.model.calls),0)
        self.flow.settings.save({'max_total_tokens':300000});self.model.unsafe=True;j=self.run_job()
        self.assertEqual(j['status'],'failed');self.assertEqual(j['usage']['total_tokens'],20);self.assertEqual(j['request_count'],2)

    def test_efficient_extraction_disabled_thinking_final_verification_preserved(self):
        j=self.run_job();self.assertEqual(j['status'],'completed',j['error'])
        actual=self.configs[1:]
        self.assertEqual(actual[0]['extra_parameters']['thinking']['type'],'disabled')
        self.assertEqual(actual[-1]['extra_parameters']['thinking']['type'],'enabled')
        self.assertTrue(all(c['max_tokens']<=8192 for c in actual))
        self.assertEqual(self.flow.settings.public()['max_tokens'],32768)

    def test_lookup_of_already_supplied_message_does_not_make_extra_call(self):
        self.model.queries=True;j=self.run_job();self.assertEqual(j['status'],'completed',j['error'])
        self.assertEqual(len(self.model.calls),2);self.assertTrue(any('未读材料' in w for w in j['warnings']))

    def test_changed_attachment_reopens_unchanged_message(self):
        folder=self.root/'assets';folder.mkdir();(folder/'messages.md').write_text(self.md.read_text('utf8')+'[附件](note.txt)\n','utf8');(folder/'note.txt').write_text('原附件','utf8')
        self.store.register_import(self.obj['id'],folder)
        p,_,summary,_=freeze_window(self.store,self.obj['id'],*bounds(self.payload))
        marks=summary['message_marks'];(folder/'note.txt').write_text('改后的附件','utf8');self.store.register_import(self.obj['id'],folder)
        _,_,summary,_=freeze_window(self.store,self.obj['id'],*bounds(self.payload),processed={'messages':marks},allow_empty=True)
        self.assertEqual(summary['analysis_messages'],1)

    def test_latest_scope_file_reuse_and_legacy_profile_defaults(self):
        j=self.run_job(range='latest');self.assertEqual(j['status'],'completed',j['error']);count=len(self.model.calls)
        j=self.run_job(range='latest');self.assertEqual(j['status'],'completed',j['error']);self.assertEqual(len(self.model.calls),count)
        path=self.flow.settings.path.parent/'model-profiles.json';data=json.loads(path.read_text('utf8'));pid=data['active']
        for p in data['profiles']:
            for k in ('efficient_mode','incremental_analysis','max_model_calls','max_total_tokens'):p.pop(k,None)
        path.write_text(json.dumps(data),'utf8');self.flow.settings.save({'profile_action':'select','profile_id':pid})
        self.assertTrue(self.flow.settings.public()['efficient_mode'])

    def test_rule_change_and_changed_analysis_goal_invalidate_reuse(self):
        self.run_job();count=len(self.model.calls)
        self.store.save_memory({'object_id':self.obj['id'],'content':'实习通知须确认截止日期','kind':'advice','match_terms':['实习'],'exceptions':'','state':'active'})
        j=self.run_job();self.assertEqual(j['status'],'completed',j['error']);self.assertGreater(len(self.model.calls),count)
        count=len(self.model.calls);j=self.run_job(supplement='进一步核对实习材料责任人')
        self.assertEqual(j['status'],'completed',j['error']);self.assertGreater(len(self.model.calls),count)

    def test_public_job_does_not_expose_large_incremental_index(self):
        j=self.flow.create(self.payload,False)
        self.assertIn('cache_candidates',self.flow.job(j['id']))
        self.assertNotIn('cache_candidates',self.flow.public_job(j['id']))

    def test_agent_no_new_data_finishes_without_manual_agent_request(self):
        self.run_job();j=self.run_job(mode='agent')
        self.assertEqual(j['status'],'completed',j['error']);self.assertIn('复用',j['phase'])

    def test_cli_agent_submission_updates_cloud_incremental_cache(self):
        from workdesk.flow import Pipeline
        j=self.flow.create({**self.payload,'mode':'agent'},False);self.flow.run(j['id']);ctx=self.flow.context(j['id'])
        Pipeline(self.store).submit(j['id'],{'matters':[],'backgrounds':[],'coverage':[dict(source_id=s['id'],state='read',note='测试已完整阅读') for s in ctx['sources']]})
        j=self.run_job();self.assertEqual(j['status'],'completed',j['error']);self.assertFalse(self.model.calls)
