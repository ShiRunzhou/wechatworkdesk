import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from workdesk.flow_config import Settings
from workdesk.model_api import ModelAPI,InvalidModelOutput
from workdesk.store import DeskError

class ResponseTests(unittest.TestCase):
    def client(self):
        with tempfile.TemporaryDirectory() as t:c=Settings(t).public()
        c.update(base_url='http://127.0.0.1:9',model='mock')
        return ModelAPI(c)
    def test_empty_response_diagnostics_and_usage(self):
        c=self.client()
        for obj,expected in [({'error':{'message':'private'}},'choices'),({'choices':[{'message':{'content':None,'reasoning_content':'private thinking'}}],'usage':{'total_tokens':7}},'仅有思考'),({'choices':[{'message':{'content':''}}]},'空正文')]:
            with patch.object(c,'_request',return_value=obj),self.assertRaises(InvalidModelOutput) as r:c.complete([])
            self.assertIn(expected,str(r.exception))
            self.assertNotIn('private',json.dumps(r.exception.details))
            self.assertEqual(r.exception.usage,obj.get('usage',{}))
    def test_keepalive_cannot_extend_total_deadline(self):
        count=[]
        class H(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));count.append(1)
                self.send_response(200);self.end_headers()
                try:
                    for _ in range(15):self.wfile.write(b'\n');self.wfile.flush();time.sleep(.08)
                except OSError:pass
        s=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=s.serve_forever,daemon=True).start()
        c=self.client();c.base='http://127.0.0.1:'+str(s.server_port);c.config['timeout']=.25
        start=time.monotonic()
        try:
            with self.assertRaisesRegex(DeskError,'超过'):c.complete([])
            self.assertLess(time.monotonic()-start,.9);self.assertEqual(len(count),1)
        finally:s.shutdown();s.server_close()

