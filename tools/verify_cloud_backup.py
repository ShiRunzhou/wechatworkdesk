"""Read-only CRC check and a trial extraction of the backed-up database."""
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

files=sorted(Path('/var/backups/wechatworkdesk').glob('wcwd-*.zip'))
if not files:raise SystemExit('No completed backup')
with zipfile.ZipFile(files[-1]) as z:
    assert z.testzip() is None
    with tempfile.TemporaryDirectory() as tmp:
        db=z.extract('workdesk.sqlite3',tmp)
        with closing(sqlite3.connect(db)) as con:
            assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            print(json.dumps({'backup_crc':'ok','restored_matters':con.execute('SELECT count(*) FROM matters').fetchone()[0],
                              'restored_sources':con.execute('SELECT count(*) FROM sources').fetchone()[0]}))
