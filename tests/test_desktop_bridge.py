import base64,json,tempfile,threading,time,unittest
from pathlib import Path
from urllib import request,error
from workdesk.cloud import WebsiteAccess,initialize_access,CloudPipeline
from workdesk.assistant import Assistant,Remote,make_server as local_server
from workdesk.server import make_server
from workdesk.store import Store

class DesktopBridgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.access_file=self.root/'access.json';initialize_access(self.access_file,'https://desk.example','tester','example-password-123')
  self.access=WebsiteAccess(self.access_file)
 def test_scoped_volatile_tokens(self):
  issued=self.access.issue_device_token();h='Bearer '+issued['token']
  for method,path in [('GET','/api/desktop/catalog'),('POST','/api/objects'),('POST','/api/uploads')]:self.assertTrue(self.access.authenticate_device(h,path,method))
  for method,path in [('GET','/api/state'),('POST','/api/desktop-token'),('POST','/api/pipeline/jobs'),('GET','/api/memory')]:self.assertFalse(self.access.authenticate_device(h,path,method))
  self.assertFalse(WebsiteAccess(self.access_file).authenticate_device(h,'/api/desktop/catalog','GET'))
  self.assertNotIn(issued['token'],self.access_file.read_text())
 def test_cloud_pairing_endpoint_requires_login_and_limits_catalog(self):
  store=Store(self.root/'data');obj=store.add_object('test');s=make_server(store,0,CloudPipeline(store),self.access);threading.Thread(target=s.serve_forever,daemon=True).start();self.addCleanup(s.server_close);self.addCleanup(s.shutdown)
  base=f'http://127.0.0.1:{s.server_port}';basic='Basic '+base64.b64encode(b'tester:example-password-123').decode()
  req=request.Request(base+'/api/desktop-token',data=b'{}',headers={'Origin':self.access.origin,'Authorization':basic,'Content-Type':'application/json'})
  token=json.load(request.urlopen(req))['token'];bearer='Bearer '+token
  value=json.load(request.urlopen(request.Request(base+'/api/desktop/catalog',headers={'Authorization':bearer})));self.assertEqual(value['objects'][0]['id'],obj['id']);self.assertEqual(list(value),['objects'])
  with self.assertRaises(error.HTTPError) as e:request.urlopen(request.Request(base+'/api/state',headers={'Authorization':bearer}))
  self.assertEqual(e.exception.code,401)
 def test_local_cors_pairing_and_no_token_persistence(self):
  class R:
   def __init__(self,c,p):self.password=p
   def call(self,*args):
    if not self.password.startswith('wcwd-device:'):raise ValueError('bad token')
    return {'objects':[]}
  a=Assistant(self.root/'assistant',remote_factory=R);a.save({'website':self.access.origin});s=local_server(a,0);threading.Thread(target=s.serve_forever,daemon=True).start();self.addCleanup(s.server_close);self.addCleanup(s.shutdown)
  base=f'http://127.0.0.1:{s.server_port}';origin=self.access.origin
  pre=request.Request(base+'/api/device/pair',method='OPTIONS',headers={'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type','Access-Control-Request-Private-Network':'true'})
  with request.urlopen(pre) as r:self.assertEqual(r.headers['Access-Control-Allow-Origin'],origin);self.assertEqual(r.headers['Access-Control-Allow-Private-Network'],'true')
  token=self.access.issue_device_token();data=json.dumps(token).encode();req=request.Request(base+'/api/device/pair',data=data,headers={'Origin':origin,'Content-Type':'application/json','Sec-Fetch-Site':'cross-site'})
  with request.urlopen(req) as r:self.assertTrue(json.load(r)['password_set'])
  self.assertNotIn(token['token'],a.path.read_text());self.assertNotIn(token['token'],json.dumps(a.public()))
  with self.assertRaises(error.HTTPError) as e:request.urlopen(request.Request(base+'/api/device/pair',data=data,headers={'Origin':'https://evil.example','Content-Type':'application/json'}))
  self.assertEqual(e.exception.code,403)
  h=Remote({'website':origin,'username':'tester'},'wcwd-device:'+token['token']).headers
  self.assertEqual(h['Authorization'],'Bearer '+token['token'])
