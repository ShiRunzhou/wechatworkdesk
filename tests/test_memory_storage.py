import copy
import json
import os
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from urllib import request,error

from workdesk.store import Store,DeskError
from workdesk.flow import Pipeline
from workdesk.server import make_server
from test_flow import FakeModel


class MemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'data')
        self.obj=self.store.add_object('模拟通知群','group','simulated')
        self.file=self.root/'chat.md';self.file.write_text('讲座报名通知，请老师按需报名。\n专业主任有明确组织任务的另行通知。\n','utf8')
        self.store.register_import(self.obj['id'],self.file)
        run=self.store.create_run([self.obj['id']],'仅分析模拟资料')
        self.sid=self.store.context(run['id'])['sources'][0]['id']
        self.payload=dict(coverage=[dict(source_id=self.sid,state='read',note='全文')],matters=[
            dict(object_id=self.obj['id'],dedup_key='simulated-lecture',title='讲座报名通知',detail='模拟通知要求老师按需报名。',action='判断是否参加',owner='本人适用性待判断',notice_at='2026-01-01T00:00:00+08:00',
                 evidence=[dict(source_id=self.sid,locator='第1行')])])
        self.store.submit(run['id'],self.payload);self.m=self.store.dashboard()['matters'][0]

    def feedback(self,m=None,**kwargs):
        m=m or self.store.dashboard()['matters'][0]
        return self.store.feedback('matter',m['id'],dict(version=m['version'],**kwargs))

    def age(self):
        stamp=(datetime.now(timezone.utc)-timedelta(days=120)).isoformat()
        with self.store.connect() as con:con.execute('UPDATE sources SET created_at=?,last_seen_at=?',(stamp,stamp))

    def clean(self):
        preview=self.store.storage_preview()
        return self.store.storage_clean(dict(token=preview['token'],ids=[s['id'] for s in preview['candidates']],confirmed=True))

    def test_ambiguous_future_note_is_candidate_not_active(self):
        self.feedback(state='irrelevant',note='以后此类信息无需列出')
        rule=self.store.memory_state()['rules'][0]
        self.assertEqual(rule['state'],'candidate');self.assertEqual(rule['origin'],'feedback')
        run=self.store.create_run([self.obj['id']],'后续模拟分析')
        self.assertEqual(self.store.context(run['id'])['objects'][0]['work_memory']['rules'],[])

    def test_explicit_rule_is_scoped_and_disable_uses_version(self):
        self.feedback(state='irrelevant',note='本次不参加',rule=dict(confirmed=True,content='此群讲座报名通常无需列出',kind='exclude',match_terms=['讲座报名'],exceptions='明确要求主任组织的仍列出'))
        rule=self.store.memory_state()['rules'][0];self.assertEqual(rule['state'],'active')
        another=self.store.add_object('另一个模拟群');self.store.register_import(another['id'],self.file)
        ctx=self.store.context(self.store.create_run([self.obj['id'],another['id']],'模拟')['id'])
        memories={o['id']:o['work_memory'] for o in ctx['objects']}
        self.assertEqual(len(memories[self.obj['id']]['rules']),1);self.assertEqual(memories[another['id']]['rules'],[])
        self.store.save_memory({**rule,'state':'disabled'})
        with self.assertRaises(DeskError):self.store.save_memory({**rule,'state':'active'})
        self.assertEqual(self.store.memory_state()['rules'][0]['state'],'disabled')

    def test_behavior_needs_distinct_matters_and_remains_candidate(self):
        for _ in range(3):self.feedback(state='irrelevant',memory_category='讲座报名')
        self.assertEqual(self.store.memory_state()['rules'],[])
        payload=copy.deepcopy(self.payload)
        payload['matters']=[{**payload['matters'][0],'dedup_key':'lecture-'+str(i),'title':'讲座报名 '+str(i)} for i in range(2)]
        run=self.store.create_run([self.obj['id']],'模拟多次判断');self.store.submit(run['id'],payload)
        for m in self.store.dashboard()['matters']:
            if m['state']!='irrelevant':self.feedback(m,state='irrelevant',memory_category='讲座报名')
        rule=self.store.memory_state()['rules'][0]
        self.assertEqual(rule['origin'],'behavior');self.assertEqual(rule['state'],'candidate');self.assertEqual(len(rule['basis']),3)
        self.store.save_memory({**rule,'state':'rejected'})
        self.feedback(state='irrelevant',memory_category='讲座报名')
        self.assertEqual(self.store.memory_state()['rules'][0]['state'],'rejected')

    def test_archive_remembers_completion_and_reopening(self):
        m=self.feedback(state='completed',memory_note='已向教学秘书反馈，并确认无需专业主任组织。')
        h=self.store.search_memory('教学秘书')[0]
        self.assertEqual(h['dedup_key'],m['dedup_key']);self.assertIn('无需专业主任组织',h['resolution'])
        self.assertTrue((self.store.root/'memory/history-2026.md').is_file())
        self.feedback(m,state='in_progress',note='新增要求，需要再处理')
        self.assertEqual(self.store.memory_state()['history'][0]['current_state'],'in_progress')

    def test_missing_completion_evidence_is_not_invented(self):
        self.feedback(state='completed')
        self.assertIn('尚未填写',self.store.memory_state()['history'][0]['resolution'])

    def test_recent_and_unfinished_sources_are_kept(self):
        self.assertEqual(self.store.storage_preview()['candidates'],[])
        self.age();self.assertEqual(self.store.storage_preview()['candidates'],[])
        self.assertEqual(self.store.storage_preview()['protected'],1)
        self.feedback(state='completed',note='已完成，另有备份')
        self.assertEqual(len(self.store.storage_preview()['candidates']),1)

    def test_source_cleanup_archives_then_reupload_restores_same_id(self):
        self.age();self.feedback(state='completed',memory_note='已确认通知无需本人执行')
        result=self.clean();self.assertEqual(result['removed'],1)
        with self.assertRaises(DeskError):self.store.source(self.sid)
        self.assertTrue(self.file.exists())
        self.assertIn('无需本人执行',self.store.search_memory('通知')[0]['resolution'])
        with self.store.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM sources').fetchone()[0],1)
        self.store.register_import(self.obj['id'],self.file)
        source,file=self.store.source(self.sid);self.assertEqual(source['cleaned_at'],'');self.assertTrue(file.exists())
        self.assertEqual(self.store.storage_preview()['candidates'],[])

    def test_stale_cleanup_preview_cannot_remove_newly_needed_source(self):
        self.age();m=self.feedback(state='completed');preview=self.store.storage_preview()
        self.feedback(m,state='in_progress')
        with self.assertRaises(DeskError):self.store.storage_clean(dict(token=preview['token'],ids=[self.sid],confirmed=True))
        self.assertTrue(self.store.source(self.sid)[1].exists())

    def test_reading_round_and_pending_background_protect_evidence(self):
        self.age();self.feedback(state='completed')
        run=self.store.create_run([self.obj['id']],'模拟尚未结束的阅读')
        self.assertEqual(self.store.storage_preview()['candidates'],[])
        payload=copy.deepcopy(self.payload);payload['matters']=[];payload['backgrounds']=[dict(object_id=self.obj['id'],summary='模拟背景',evidence=[dict(source_id=self.sid,locator='第1行')])]
        self.store.submit(run['id'],payload)
        self.assertEqual(self.store.storage_preview()['candidates'],[])

    def test_failed_unlink_can_retry_and_backup_omits_tombstone(self):
        self.age();self.feedback(state='completed')
        with patch.object(Path,'unlink',side_effect=PermissionError('模拟文件被占用')):
            result=self.clean()
        self.assertEqual(result['failed'],[self.sid]);self.assertEqual(len(self.store.storage_preview()['candidates']),1)
        self.assertEqual(self.clean()['removed'],1)
        from test_cloud_backup import tool
        archive=tool('backup_cloud').backup(self.store.root,self.root/'backup')
        with zipfile.ZipFile(archive) as z:self.assertFalse(any(name.startswith('sources/') for name in z.namelist()))

    def test_old_task_materials_are_compacted_and_active_tasks_are_kept(self):
        self.feedback(state='completed');jobs=self.store.root/'flow/jobs';old=(datetime.now(timezone.utc)-timedelta(days=120)).timestamp()
        for jid,status in [('a'*32,'completed'),('b'*32,'running')]:
            folder=jobs/jid;folder.mkdir(parents=True);file=folder/'state.json'
            file.write_text(json.dumps(dict(id=jid,status=status,run_id=None)),'utf8');os.utime(file,(old,old))
            (folder/'messages.md').write_text('模拟原始正文','utf8')
        self.assertEqual(self.clean()['compacted_jobs'],1)
        self.assertFalse((jobs/('a'*32)/'messages.md').exists());self.assertTrue((jobs/('b'*32)/'messages.md').exists())
        self.assertTrue(json.loads((jobs/('a'*32)/'state.json').read_text('utf8'))['materials_cleaned_at'])

    def test_auto_cleanup_requires_enable_and_runs_once_per_day(self):
        self.age();self.feedback(state='completed')
        self.assertIsNone(self.store.storage_auto())
        self.store.save_storage_policy(dict(retention_days=90,auto_enabled=True))
        self.assertEqual(self.store.storage_auto()['removed'],1);self.assertIsNone(self.store.storage_auto())

    def test_model_rule_proposal_requires_actual_human_feedback(self):
        self.feedback(state='irrelevant',note='平常不用转达')
        fid=self.store.memory_state()['rules'][0]['basis'][0]['feedback_id']
        run=self.store.create_run([self.obj['id']],'模拟模型规则提炼');payload=copy.deepcopy(self.payload);payload['matters']=[]
        payload['rule_suggestions']=[dict(object_id=self.obj['id'],content='群通知通常无需再次转达',kind='advice',match_terms=[],exceptions='明确要求转达除外',feedback_ids=[fid])]
        self.store.submit(run['id'],payload)
        self.assertTrue(any(r['origin']=='model' and r['state']=='candidate' for r in self.store.memory_state()['rules']))
        run=self.store.create_run([self.obj['id']],'模拟捏造反馈');payload['rule_suggestions'][0]['feedback_ids']=['f'*32]
        with self.assertRaises(DeskError):self.store.submit(run['id'],payload)

    def test_filtered_results_require_active_rule_and_matching_version(self):
        self.feedback(state='irrelevant',rule=dict(confirmed=True,content='普通讲座报名无需列出',kind='exclude',match_terms=['讲座报名'],exceptions='有组织任务除外'))
        rule=self.store.memory_state()['rules'][0];payload=copy.deepcopy(self.payload);payload['matters']=[]
        payload['filtered_items']=[dict(object_id=self.obj['id'],rule_id=rule['id'],rule_version=rule['version'],title='普通讲座报名',reason='无明确组织任务，符合规则',evidence=[dict(source_id=self.sid,locator='第1行')])]
        run=self.store.create_run([self.obj['id']],'模拟规则过滤');self.store.submit(run['id'],payload)
        self.assertEqual(len(self.store.memory_state()['filtered']),1)
        self.store.save_memory({**rule,'state':'disabled'})
        run=self.store.create_run([self.obj['id']],'模拟过期规则')
        with self.assertRaises(DeskError):self.store.submit(run['id'],payload)

    def test_memory_and_cleanup_routes_are_available(self):
        server=make_server(self.store,0);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        for path in ('/memory.js','/api/memory','/api/storage','/api/memory/download'):
            with request.urlopen(base+path) as response:self.assertEqual(response.status,200)
        req=request.Request(base+'/api/memory',data=json.dumps({'profile':'模拟：负责专业日常工作'}).encode(),headers={'Content-Type':'application/json'})
        with request.urlopen(req) as response:self.assertEqual(json.load(response)['profile'],'模拟：负责专业日常工作')

    def test_archive_does_not_promote_labelled_credentials_to_memory(self):
        self.feedback(state='completed',note='已完成核对\n密码：模拟敏感内容')
        archive=self.store.memory_state()['history'][0]
        self.assertNotIn('模拟敏感内容',archive['resolution']);self.assertIn('凭据省略',archive['resolution'])

    def test_reopened_matter_restores_failed_cleanup_file(self):
        self.age();m=self.feedback(state='completed')
        with patch.object(Path,'unlink',side_effect=PermissionError('模拟占用')):self.clean()
        self.feedback(m,state='in_progress')
        self.assertTrue(self.store.source(self.sid)[1].is_file())
        self.assertEqual(self.store.storage_preview()['candidates'],[])

    def test_api_proposals_and_filtering_end_to_end(self):
        self.feedback(state='irrelevant',note='以后此群讲座通常无需我参加')
        self.store.save_memory(dict(object_id=self.obj['id'],content='普通讲座报名无需列出',kind='exclude',match_terms=['讲座报名'],exceptions='明确组织任务除外',state='active'))
        rule=next(r for r in self.store.memory_state()['rules'] if r['state']=='active')
        outer=self
        class Model:
            def complete(self,messages):
                data=json.loads(messages[-1]['content']);context=data['context']
                fid=context['work_memory']['feedback'][0]['id']
                self_sid=next(s['id'] for s in flow.context(job['id'])['sources'] if s['id'] in flow.context(job['id'])['pipeline']['primary_sources'])
                return dict(matters=[],backgrounds=[],lookup_requests=[],rule_suggestions=[dict(content='通知群通常无需重复转达',kind='advice',match_terms=[],exceptions='明确要求除外',feedback_ids=[fid])],filtered_items=[dict(title='普通讲座报名',rule_id=rule['id'],rule_version=rule['version'],reason='依据明确规则，原文未要求本人组织',evidence=[dict(source_id=self_sid,locator='第1行')])]),{}
        from workdesk.cloud import CloudPipeline
        flow=CloudPipeline(self.store,provider_factory=lambda c,k:Model())
        flow.settings.save({'base_url':'http://127.0.0.1:1234/v1','model':'simulated'})
        job=flow.create(dict(sessions=[self.obj['id']],mode='api'),background=False);flow.run(job['id'])
        self.assertEqual(flow.job(job['id'])['status'],'completed',flow.job(job['id']).get('error'))
        memory=self.store.memory_state();self.assertEqual(len(memory['filtered']),1)
        self.assertTrue(any(r['origin']=='model' and r['state']=='candidate' for r in memory['rules']))
        self.assertEqual(self.store.dashboard()['matters'][0]['state'],'irrelevant')

    def test_cancelled_agent_round_releases_retention_protection(self):
        from workdesk.cloud import CloudPipeline
        self.age();self.feedback(state='completed')
        flow=CloudPipeline(self.store);job=flow.create(dict(sessions=[self.obj['id']],mode='agent'),background=False);flow.run(job['id'])
        self.assertEqual(self.store.storage_preview()['candidates'],[])
        flow.stop(job['id']);self.assertEqual(len(self.store.storage_preview()['candidates']),1)
        with self.assertRaises(DeskError):self.store.submit(job['run_id'],self.payload)

    def test_cleanup_rejects_paths_outside_snapshot_directory(self):
        self.age();self.feedback(state='completed')
        with self.store.connect() as con:con.execute('UPDATE sources SET snapshot_path=? WHERE id=?',(str(self.file),self.sid))
        with self.assertRaises(DeskError):self.clean()
        self.assertTrue(self.file.exists())

    def test_changed_completed_matter_keeps_old_resolution_and_raw_evidence(self):
        self.age();self.feedback(state='completed',memory_note='原要求已办理完成')
        payload=copy.deepcopy(self.payload);payload['matters'][0]['detail']='后续分析提出新的要求，待核对'
        run=self.store.create_run([self.obj['id']],'模拟后续变更');self.store.submit(run['id'],payload)
        self.store.scan_feedback()
        h=self.store.memory_state()['history'][0]
        self.assertEqual(h['current_state'],'completed_pending_review');self.assertNotIn('新的要求',h['summary'])
        self.assertEqual(self.store.storage_preview()['candidates'],[])

    def test_archive_export_failure_prevents_physical_cleanup(self):
        self.age();self.feedback(state='completed')
        with patch.object(self.store,'export_memory',side_effect=OSError('模拟档案写入失败')):
            with self.assertRaises(OSError):self.clean()
        self.assertTrue(self.store.source(self.sid)[1].exists())

if __name__=='__main__':unittest.main()
