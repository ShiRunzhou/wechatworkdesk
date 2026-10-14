"""Isolated canonical-site and desktop-gateway fixture; never reads real chats."""
import json
import sys
import tempfile
import threading
from pathlib import Path
from urllib import request
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from workdesk.store import Store
from workdesk.cloud import CloudPipeline,WebsiteAccess,initialize_access
from workdesk.server import make_server
from workdesk.assistant import Assistant,make_server as device_server
from workdesk.chat_update import TZ
from test_assistant import FakeClient
from test_flow import FakeModel

temp=tempfile.TemporaryDirectory(prefix='wcwd-integrated-ui-');root=Path(temp.name)
store=Store(root/'canonical');obj=store.add_object('模拟联系人','person','alice')
md=root/'messages.md';md.write_text('## '+datetime.now(TZ).isoformat()+' 甲\n消息标识: server:initial\n请核对材料。\n','utf8')
flow=CloudPipeline(store,provider_factory=lambda c,k:FakeModel())
flow.upload(md,'messages.md',obj['id']);flow.settings.save({'base_url':'http://127.0.0.1:1234/v1','model':'fixture-only'})
access=root/'site.json';initialize_access(access,'https://desk.plantisland.cn','fixture','fixture-password-only')
site=make_server(store,8879,flow,WebsiteAccess(access));threading.Thread(target=site.serve_forever,daemon=True).start()
real_opener=request.build_opener
class BridgeOpener:
    def open(self,req,timeout=180):
        url=req.full_url.replace('https://desk.plantisland.cn','http://127.0.0.1:8879',1)
        forwarded=request.Request(url,data=req.data,headers=dict(req.header_items()),method=req.get_method())
        return real_opener(request.ProxyHandler({})).open(forwarded,timeout=timeout)
request.build_opener=lambda *a,**k:BridgeOpener()
assistant=Assistant(root/'assistant',client_factory=FakeClient);assistant.config.update(username='fixture',export_directory=str(root/'exports'),selection=['alice'])
assistant.password='fixture-password-only';assistant.refresh();assistant.connect()
server=device_server(assistant,8880)
print('Fixture desktop http://127.0.0.1:8880/update; website http://127.0.0.1:8879/update',flush=True)
try:server.serve_forever()
finally:server.server_close();site.shutdown();site.server_close();temp.cleanup()
