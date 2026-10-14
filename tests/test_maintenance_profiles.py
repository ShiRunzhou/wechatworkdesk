import json, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from workdesk.store import Store, DeskError
from workdesk.flow_config import Settings

class NewFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data');self.obj=self.store.add_object('模拟','group')
        f=self.root/'source.md';f.write_text('模拟通知',encoding='utf8')
        self.store.register_import(self.obj['id'],f)
        self.run=self.store.create_run([self.obj['id']],'模拟测试')
        ctx=self.store.context(self.run['id']);sid=ctx['sources'][0]['id']
        self.payload={'coverage':[{'source_id':sid,'state':'read','note':'全文'}], 'matters':[{
            'object_id':self.obj['id'],'dedup_key':'demo','title':'模拟事项','detail':'模拟详细内容',
            'action':'模拟操作','notice_at':'2026-01-01T00:00:00+08:00','suggested_state':'pending',
            'evidence':[{'source_id':sid,'locator':'第一行'}]}]}
        self.store.submit(self.run['id'],self.payload)
        self.m=self.store.dashboard()['matters'][0]
    def tearDown(self):self.tmp.cleanup()
    def old_completed(self):
        m=self.store.feedback('matter',self.m['id'],{'version':self.m['version'],'state':'completed'})
        old=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        with self.store.connect() as con:con.execute('UPDATE matters SET completed_at=? WHERE id=?',(old,m['id']))
        return m
    def test_cleanup_keeps_ledger_and_prevents_resurrection(self):
        m=self.old_completed();preview=self.store.cleanup_preview()
        self.assertEqual(len(preview['candidates']),1)
        result=self.store.cleanup_confirm({'confirmed':True,'items':[{'id':m['id'],'version':m['version']}]})
        self.assertEqual(result['cleaned'],1);self.assertEqual(self.store.dashboard()['matters'],[])
        log=self.store.cleanup_preview()['logs'][0]
        self.assertEqual(log['reference_code'],m['reference_code']);self.assertEqual(log['title'],m['title'])
        self.assertNotIn('detail',log)
        run=self.store.create_run([self.obj['id']],'再次分析');self.store.submit(run['id'],self.payload)
        self.assertEqual(self.store.dashboard()['matters'],[])
    def test_recent_completion_and_stale_selection_cannot_be_cleaned(self):
        m=self.store.feedback('matter',self.m['id'],{'version':self.m['version'],'state':'completed'})
        self.assertEqual(self.store.cleanup_preview()['candidates'],[])
        with self.assertRaises(DeskError):self.store.cleanup_confirm({'confirmed':True,'items':[{'id':m['id'],'version':m['version']}]})
        old=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        with self.store.connect() as con:con.execute('UPDATE matters SET completed_at=? WHERE id=?',(old,m['id']))
        self.store.feedback('matter',m['id'],{'version':m['version'],'state':'in_progress'})
        with self.assertRaises(DeskError):self.store.cleanup_confirm({'confirmed':True,'items':[{'id':m['id'],'version':m['version']}]})
        self.assertEqual(self.store.cleanup_preview()['logs'],[])
    def test_profiles_switch_preserve_independent_settings(self):
        cfg=Settings(self.root/'config');first=cfg.public()['active_profile']
        cfg.save({'profile_name':'本地','base_url':'http://127.0.0.1:1234/v1','model':'local-model'})
        second=cfg.save({'profile_action':'create','profile_name':'远程'})['active_profile']
        self.assertNotEqual(first,second)
        cfg.save({'base_url':'https://example.com/v1','model':'remote-model'})
        a=cfg.save({'profile_action':'select','profile_id':first});self.assertEqual(a['model'],'local-model')
        b=cfg.save({'profile_action':'select','profile_id':second});self.assertEqual(b['model'],'remote-model')
        self.assertEqual(len(b['profiles']),2)
        if os.name=='nt':
            cfg.save({'api_key':'private-test-secret'});cfg.save({'profile_action':'select','profile_id':first})
            self.assertEqual(cfg.key(),'');cfg.save({'profile_action':'select','profile_id':second})
            self.assertEqual(cfg.key(),'private-test-secret')
            self.assertNotIn('private-test-secret',json.dumps(cfg.public()))
            self.assertNotIn('encrypted_key',json.dumps(cfg.public()))

    def test_device_paths_are_independent_of_model_profiles(self):
        from workdesk.device_paths import save_paths
        cfg=Settings(self.root/'config')
        fake=self.root/'WeFlow-config.json';fake.write_text('{}',encoding='utf8')
        export=self.root/'custom-records'
        saved=save_paths(cfg,{'weflow_config':str(fake),'weflow_executable':'','export_directory':str(export)})
        cfg.save({'profile_action':'create','profile_name':'另一模型'})
        self.assertEqual(cfg.public()['device_paths'],saved)
        self.assertFalse(export.exists())
        with self.assertRaises(DeskError):save_paths(cfg,{'export_directory':'relative/path'})

    def test_custom_date_includes_entire_day_and_accepts_same_day(self):
        from workdesk.flow import Pipeline
        from workdesk.chat_update import TZ
        flow=Pipeline(self.store,export=self.root/'records',client_factory=lambda scope:None)
        flow.catalog['demo']={'external_id':'demo','name':'模拟','kind':'person'}
        day=(datetime.now(TZ)-timedelta(days=2)).date().isoformat()
        job=flow.create({'sessions':['demo'],'mode':'update','range':'custom','start':day,'end':day},background=False)
        self.assertTrue(job['start'].endswith('00:00:00+08:00'))
        self.assertTrue(job['end'].endswith('23:59:59.999999+08:00'))
        tomorrow=(datetime.now(TZ)+timedelta(days=1)).date().isoformat()
        with self.assertRaises(DeskError):flow.create({'sessions':['demo'],'mode':'update','range':'custom','start':day,'end':tomorrow},background=False)
