import json
import tempfile
import unittest
import zipfile
import threading
from datetime import datetime
from pathlib import Path
from urllib import request,error
from workdesk.assistant import Assistant,Remote,VoiceClient,time_range,package_inventory,make_server
from workdesk.chat_update import TZ

class FakeClient:
    pages=0
    def __init__(self,*args,**kwargs):pass
    def get(self,path,params):
        return {'contacts':[{'username':'alice','nickname':'甲'},{'username':'g@chatroom','nickname':'群'}]} if path.endswith('contacts') else {'sessions':[{'username':'alice','displayName':'甲'}]}
    def names(self,t):return {}
    def page(self,t,start,end,offset,limit):
        FakeClient.pages+=1
        return {'talker':t,'hasMore':False,'messages':[{'serverId':'1','localId':1,'createTime':end-10,'sortSeq':1,'localType':1,'content':'需要处理','senderUsername':t}]}

class FakeRemote:
    uploaded=[];fail=False;objects=[];analyses=0
    def __init__(self,*args):pass
    def call(self,path,payload=None,file=None):
        if path=='/api/state':return {'objects':self.objects.copy()}
        if path=='/api/objects':
            obj={'id':str(len(self.objects)+1).zfill(32),**payload};self.objects.append(obj);return obj
        if path.startswith('/api/uploads'):
            if self.fail:raise ValueError('模拟网络故障')
            with zipfile.ZipFile(file) as z:self.uploaded.append(z.read('messages.md').decode())
            return {'added':1,'skipped':0}
        if path=='/api/pipeline/jobs':FakeRemote.analyses+=1;return {'id':'analysis'}
        raise AssertionError(path)

class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        FakeRemote.uploaded=[];FakeRemote.objects=[];FakeRemote.fail=False;FakeRemote.analyses=0;FakeClient.pages=0
        self.a=Assistant(self.root/'assistant',FakeClient,FakeRemote)
        self.a.save({'export_directory':str(self.root/'exports'),'password':'private-value'})
        self.a.refresh()
    def test_range_default_and_calendar(self):
        now=datetime(2026,10,14,12,30,tzinfo=TZ);s,e=time_range({},now)
        self.assertEqual(e-s,7*86400)
        s,e=time_range({'range':'custom','start':'2026/10/01','end':'2026/10/01'})
        self.assertEqual(e-s,86399)
        for value in ['文字','2026/02/30','2026-10-01']:
            with self.assertRaises(ValueError):time_range({'range':'custom','start':value,'end':value})
    def test_contacts_dedup_and_no_secret_on_disk(self):
        self.assertEqual(len(self.a.catalog),2)
        self.assertNotIn('private-value',self.a.path.read_text())
        self.assertNotIn('private-value',json.dumps(self.a.public()))
        self.a.start({'selection':['alice'],'mode':'export'},False)
        j=self.a.public()['jobs'][0];self.assertEqual(j['status'],'completed')
        self.assertEqual(j['conversations'][0]['added'],1)
        self.assertEqual(self.a.config['selection'],['alice'])
    def test_upload_failure_retries_without_reexport(self):
        FakeRemote.fail=True
        j=self.a.start({'selection':['alice'],'mode':'upload'},False)
        self.assertEqual(j['status'],'partial');self.assertEqual(FakeClient.pages,1)
        FakeRemote.fail=False;self.a.start({'retry':j['id']},False)
        self.assertEqual(FakeClient.pages,1);self.assertEqual(len(FakeRemote.objects),1)
        self.assertEqual(len(FakeRemote.uploaded),1)
        self.assertEqual(self.a.public()['jobs'][0]['status'],'completed')
    def test_upload_analysis_once_and_old_selection_retained_on_fail(self):
        old=self.a.config['selection'].copy();FakeRemote.fail=True
        j=self.a.start({'selection':['alice'],'mode':'agent'},False)
        self.assertEqual(self.a.config['selection'],old)
        FakeRemote.fail=False;self.a.start({'retry':j['id']},False)
        self.assertEqual(FakeRemote.analyses,1)
        with self.assertRaises(ValueError):self.a.start({'retry':j['id']},False)
    def test_reupload_keeps_window_body_and_transcript_no_audio(self):
        export=self.root/'exports';export.mkdir();img=export/'pic.png';img.write_bytes(b'png');audio=export/'v.wav';audio.write_bytes(b'wav')
        p=self.root/'inventory.json';p.write_text(json.dumps({'external_id':'alice','start':1,'end':2,'messages':{'id':{'time':2,'body':'正文','sender':'甲','kind':34,'voice_text':'后来补出','attachments':[str(img),str(audio)]}}}))
        package_inventory(p,self.root/'p.zip',export)
        with zipfile.ZipFile(self.root/'p.zip') as z:
            self.assertEqual(len(z.namelist()),2);self.assertIn('后来补出',z.read('messages.md').decode());self.assertFalse(any(n.endswith('.wav') for n in z.namelist()))
    def test_external_attachment_and_bad_paths_rejected(self):
        with self.assertRaises(ValueError):self.a.save({'export_directory':str(self.a.root)})
        with self.assertRaises(ValueError):self.a.save({'website':'http://example.com'})
        with self.assertRaises(ValueError):self.a.start({'selection':['../evil']},False)
        with self.assertRaises(ValueError):Remote({'website':'https://a:b@example.com','username':'x'},'y')
    def test_transcription_stays_cached_without_audio_file(self):
        from unittest.mock import patch
        client=object.__new__(VoiceClient);client.stt={'stt_url':'http://127.0.0.1:8000/v1','stt_model':'local'};client.stt_key='key';client.transcripts={};client.cache=self.root/'stt';client.cache.mkdir()
        msg={'serverId':'22','localId':22,'createTime':2,'localType':34,'mediaUrl':'local','mediaFileName':'voice.wav'}
        client.get=lambda *a,**k:{'messages':[msg]};client.media=lambda _:b'audio'
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b'{"text":"transcribed"}'
        class Opener:
            calls=0
            def open(self,req,timeout):Opener.calls+=1;return Response()
        with patch('workdesk.assistant.request.build_opener',return_value=Opener()):
            client.page('alice',1,3,0,200);client.page('alice',1,3,0,200)
        self.assertEqual(Opener.calls,1);self.assertEqual(client.transcripts['alice_2_22'],'transcribed')
        self.assertEqual([p.suffix for p in client.cache.iterdir()],['.json'])
    def test_ambiguous_analysis_response_not_reissued(self):
        class LostRemote(FakeRemote):
            def call(self,path,payload=None,file=None):
                if path=='/api/pipeline/jobs':FakeRemote.analyses+=1;raise ValueError('response lost')
                return super().call(path,payload,file)
        self.a.remote_factory=LostRemote
        j=self.a.start({'selection':['alice'],'mode':'api'},False)
        self.assertEqual(j['status'],'partial');self.a.start({'retry':j['id']},False)
        self.assertEqual(FakeRemote.analyses,1)
        self.assertIn('未确认',self.a.public()['jobs'][0]['phase'])
    def test_loopback_origin_required(self):
        server=make_server(self.a,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}'
        with request.urlopen(url+'/api/state') as r:self.assertEqual(r.status,200)
        req=request.Request(url+'/api/contacts',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://evil.example'})
        with self.assertRaises(error.HTTPError) as e:request.urlopen(req)
        self.assertEqual(e.exception.code,403)
        req=request.Request(url+'/api/contacts',data=b'{}',headers={'Content-Type':'application/json','Origin':url})
        with request.urlopen(req) as r:self.assertEqual(r.status,200)
    def test_polling_uses_snapshot_without_opening_job_files(self):
        from unittest.mock import patch
        j=self.a.start({'selection':['alice'],'mode':'export'},False)
        with patch('workdesk.local_voice.status',return_value={}),patch.object(Path,'read_text',side_effect=AssertionError('Job files must not be reopened on polling')):
            state=self.a.public()
        state['jobs'][0]['status']='modified-by-caller'
        self.assertEqual(self.a.public()['jobs'][0]['status'],'completed')
    def test_unwritable_progress_does_not_leave_running_task(self):
        from unittest.mock import patch
        with patch('workdesk.assistant.atomic_json',side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):self.a.start({'selection':['alice'],'mode':'export'},False)
        self.assertFalse(self.a.running)
        self.assertEqual(self.a.public()['jobs'][0]['status'],'partial')
    def test_final_progress_write_failure_is_visible_and_retryable(self):
        from unittest.mock import patch
        j=self.a.start({'selection':['alice'],'mode':'export'},False)
        self.a.running=True
        with patch('workdesk.assistant.atomic_json',side_effect=PermissionError('locked')):
            self.a.run(j)
        self.assertFalse(self.a.running)
        self.assertEqual(self.a.public()['jobs'][0]['status'],'partial')
    def test_atomic_rename_retries_transient_windows_file_lock(self):
        from unittest.mock import patch
        from workdesk.flow_config import atomic_json
        import os
        fail=PermissionError('sharing violation');fail.winerror=32
        with patch('workdesk.flow_config.os.replace',side_effect=[fail,None]) as replace,patch('workdesk.flow_config.time.sleep'):
            atomic_json(self.root/'state.json',{'ok':True})
        self.assertEqual(replace.call_count,2)
    def test_atomic_rename_permanent_error_keeps_existing_file(self):
        from unittest.mock import patch
        from workdesk.flow_config import atomic_json
        p=self.root/'state.json';p.write_text('{"old":true}')
        fail=PermissionError('locked');fail.winerror=5
        with patch('workdesk.flow_config.os.replace',side_effect=fail),patch('workdesk.flow_config.time.sleep'):
            with self.assertRaises(PermissionError):atomic_json(p,{'new':True})
        self.assertEqual(json.loads(p.read_text()),{'old':True})
        self.assertFalse(list(self.root.glob('.write-*')))

if __name__=='__main__':unittest.main()
