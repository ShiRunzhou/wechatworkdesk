"""Private canonical-data backup; immutable snapshots and a SQLite online backup."""
import argparse
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

def backup(root,output,retain=3):
    root=Path(root).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    os.chmod(output,0o700)
    name=datetime.now(timezone.utc).strftime('wcwd-%Y%m%dT%H%M%S')+'.zip'
    final=output/name;partial=output/(name+'.partial')
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dbpath=Path(tmp)/'workdesk.sqlite3'
            with closing(sqlite3.connect((root/'workdesk.sqlite3').as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dbpath)) as dst:src.backup(dst)
            with zipfile.ZipFile(partial,'x',zipfile.ZIP_DEFLATED) as z:
                z.write(dbpath,'workdesk.sqlite3')
                with closing(sqlite3.connect(dbpath)) as con:
                    columns={r[1] for r in con.execute('PRAGMA table_info(sources)')}
                    rows=con.execute("SELECT snapshot_path FROM sources"+(" WHERE cleaned_at=''" if 'cleaned_at' in columns else '')).fetchall()
                for raw, in rows:
                    file=Path(raw).resolve()
                    if not file.is_relative_to(root/'sources') or not file.is_file():raise ValueError('资料快照缺失，备份未完成')
                    z.write(file,file.relative_to(root).as_posix())
                for name in ('settings.json','model-profiles.json'):
                    file=root/'flow'/name
                    if file.is_file():z.write(file,'flow/'+name)
                z.writestr('backup-info.json',json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),'note':'恢复相同数据路径；单独保存网站认证和模型加密主密钥；不恢复运行任务。'}))
        os.chmod(partial,0o600);partial.replace(final)
        # Only rotate archives created by this tool after a new backup is complete.
        for old in sorted(output.glob('wcwd-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)[retain:]:
            if old.is_file() and not old.is_symlink():old.unlink()
        return final
    finally:partial.unlink(missing_ok=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(backup(a.data,a.output))
