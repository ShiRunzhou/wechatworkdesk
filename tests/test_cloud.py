import base64
import json
import tempfile
import os
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib import request,error
from workdesk.cloud import CloudPipeline, WebsiteAccess, initialize_access, unpack_upload
from workdesk.store import Store,DeskError
from workdesk.server import make_server
from test_flow import FakeModel

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'data')
        self.model=FakeModel();self.flow=CloudPipeline(self.store,provider_factory=lambda c,k:self.model)
        self.obj=self.store.add_object('模拟工作群')
        self.md=self.root/'chat.md';self.md.write_text('# 工作群\n请核对实习材料，10月20日前提交。\n','utf8')

    def test_upload_reuses_snapshot_and_preserves_history(self):
        first=self.flow.upload(self.md,'chat.md',self.obj['id'])
        self.flow.upload(self.md,'chat.md',self.obj['id'])
        self.assertEqual(len(self.store.dashboard()['imports']),2)
        with self.store.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM sources').fetchone()[0],1)
        self.md.write_text('新增通知：实习材料已有补充。','utf8')
        self.flow.upload(self.md,'chat.md',self.obj['id'])
        job=self.flow.create({'sessions':[self.obj['id']],'mode':'agent'},background=False)
        self.flow.run(job['id']);ctx=self.flow.context(job['id'])
        self.assertEqual(self.flow.job(job['id'])['status'],'waiting_agent')
        self.assertEqual(len(ctx['sources']),2)
        self.assertEqual(len(ctx['pipeline']['primary_sources']),1)
        self.assertEqual(self.flow.sessions(True)['sessions'][0]['external_id'],self.obj['id'])

    def test_uploaded_api_analysis_preserves_manual_status(self):
        self.flow.upload(self.md,'chat.md',self.obj['id'])
        self.flow.settings.save({'base_url':'http://127.0.0.1:1234/v1','model':'mock'})
        payload={'sessions':[self.obj['id']],'mode':'api'}
        job=self.flow.create(payload,background=False);self.flow.run(job['id'])
        self.assertEqual(self.flow.job(job['id'])['status'],'completed',self.flow.job(job['id']).get('error'))
        matter=self.store.dashboard()['matters'][0]
        self.store.feedback('matter',matter['id'],{'version':matter['version'],'state':'in_progress','note':'人工反馈'})
        job=self.flow.create(payload,background=False);self.flow.run(job['id'])
        current=self.store.dashboard()['matters'][0]
        self.assertEqual(current['reference_code'],matter['reference_code'])
        self.assertEqual(current['state'],'in_progress')
        self.assertEqual(len(self.store.dashboard()['matters']),1)

    def test_zip_rejects_traversal_links_and_duplicates(self):
        cases=[['../outside.md'],['/absolute.md'],['C:/outside.md'],['a.md','A.md']]
        for names in cases:
            archive=self.root/'bad.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for name in names:z.writestr(name,'不应写出')
            dest=self.root/'dest';dest.mkdir(exist_ok=True)
            with self.assertRaises(DeskError):unpack_upload(archive,dest)
            self.assertFalse(list(dest.iterdir()))
        with zipfile.ZipFile(archive,'w') as z:
            i=zipfile.ZipInfo('link.md');i.create_system=3;i.external_attr=0o120777<<16;z.writestr(i,'/etc/passwd')
        with self.assertRaises(DeskError):unpack_upload(archive,dest)

    def test_zip_keeps_relative_attachments(self):
        archive=self.root/'good.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('group/chat.md','![图](images/1.png)');z.writestr('group/images/1.png',b'fake')
        self.flow.upload(archive,'good.zip',self.obj['id'])
        with self.store.connect() as con:
            paths={r[0] for r in con.execute('SELECT relative_path FROM sources')}
        self.assertEqual(paths,{'group/chat.md','group/images/1.png'})

    def test_auth_and_same_origin_protect_uploads_and_downloads(self):
        path=self.root/'website.json';initialize_access(path,'https://42.193.174.4','tester','simulated-password-only')
        access=WebsiteAccess(path);server=make_server(self.store,0,self.flow,access)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        auth='Basic '+base64.b64encode(b'tester:simulated-password-only').decode()
        for route in ('/','/api/state','/api/pipeline/config','/api/sources/'+'a'*32+'/download'):
            with self.assertRaises(error.HTTPError) as err:request.urlopen(base+route)
            self.assertEqual(err.exception.code,401)
        headers={'Authorization':auth,'Origin':'https://evil.example','Content-Type':'application/octet-stream'}
        req=request.Request(base+'/api/uploads?object_id='+self.obj['id']+'&filename=chat.md',data=self.md.read_bytes(),headers=headers)
        with self.assertRaises(error.HTTPError) as err:request.urlopen(req)
        self.assertEqual(err.exception.code,403)
        headers['Origin']='https://42.193.174.4'
        with request.urlopen(request.Request(req.full_url,data=self.md.read_bytes(),headers=headers)) as response:
            self.assertGreater(json.load(response)['file_count'],0)
        with request.urlopen(request.Request(base+'/api/pipeline/config',headers={'Authorization':auth})) as response:
            self.assertTrue(json.load(response)['website_mode'])
        self.assertFalse(access.authenticate('Basic '+base64.b64encode(b'tester:wrong').decode()))

    @unittest.skipIf(os.name=='nt','Linux网站密钥加密测试')
    def test_linux_model_profiles_encrypt_separate_keys(self):
        from cryptography.fernet import Fernet
        key=self.root/'model.key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
        with patch.dict(os.environ,{'WCWD_SECRET_KEY_FILE':str(key)}):
            self.flow.settings.save({'api_key':'mock-first-key'})
            first=self.flow.settings.public()['active_profile']
            self.flow.settings.save({'profile_action':'create','profile_name':'另一个模拟接口'})
            self.flow.settings.save({'api_key':'mock-second-key'})
            self.assertEqual(self.flow.settings.key(),'mock-second-key')
            self.flow.settings.save({'profile_action':'select','profile_id':first})
            self.assertEqual(self.flow.settings.key(),'mock-first-key')
            saved=(self.store.root/'flow/model-profiles.json').read_text('utf8')
            self.assertNotIn('mock-first-key',saved);self.assertNotIn('mock-second-key',saved)
            self.assertNotIn('encrypted_key',self.flow.settings.public())

if __name__=='__main__':unittest.main()
