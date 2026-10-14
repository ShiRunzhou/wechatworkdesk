"""Read-only deployment checks. Password arrives on stdin and is never printed."""
import base64
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib import request,error

root=Path('/var/lib/wechatworkdesk')
password=sys.stdin.readline().rstrip('\r\n')
headers={'Authorization':'Basic '+base64.b64encode(('shirunzhou:'+password).encode()).decode()}
base='http://127.0.0.1:8770'
try:request.urlopen(base+'/api/state')
except error.HTTPError as e:
    assert e.code==401
else:raise AssertionError('Anonymous access must be refused')
for route in ('/','/update','/api/state','/api/pipeline/config'):
    with request.urlopen(request.Request(base+route,headers=headers)) as response:assert response.status==200
with closing(sqlite3.connect(root/'workdesk.sqlite3')) as con:
    assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    counts={table:con.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('objects','matters','feedback','sources')}
    rows=con.execute("SELECT snapshot_path,sha256 FROM sources WHERE cleaned_at='' ").fetchall()
for raw,expected in rows:
    file=Path(raw)
    assert file.resolve().is_relative_to(root/'sources')
    with file.open('rb') as source:
        h=hashlib.sha256()
        while block:=source.read(1024**2):h.update(block)
    assert h.hexdigest()==expected,'Source hash mismatch'
print(json.dumps({'authenticated_pages':'ok','anonymous_access':'refused','database':'ok','verified_counts':counts}))
