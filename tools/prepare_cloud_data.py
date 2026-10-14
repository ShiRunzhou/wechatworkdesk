"""Export canonical data consistently; do not migrate device paths or DPAPI keys."""
import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

def prepare(source, destination, target='/var/lib/wechatworkdesk'):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination.exists():raise ValueError('目标目录已存在，请使用新的目录')
    destination.mkdir(parents=True)
    try:
        with closing(sqlite3.connect(source/'workdesk.sqlite3')) as src, closing(sqlite3.connect(destination/'workdesk.sqlite3')) as dst:
            src.backup(dst)
        with closing(sqlite3.connect(destination/'workdesk.sqlite3')) as db:
            columns={r[1] for r in db.execute('PRAGMA table_info(sources)')}
            rows=db.execute('SELECT id,snapshot_path,'+('cleaned_at' if 'cleaned_at' in columns else "'' AS cleaned_at")+' FROM sources').fetchall()
            for sid,raw,cleaned_at in rows:
                if cleaned_at:continue
                file=Path(raw).resolve()
                if not file.is_relative_to(source/'sources') or not file.is_file():
                    raise ValueError('资料快照缺失或超出原数据目录，停止迁移')
                relative=file.relative_to(source)
                if len(relative.name.encode('utf8'))>200:
                    relative=relative.parent/('snapshot'+file.suffix.lower())
                out=destination/relative;out.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(file,out)
                db.execute('UPDATE sources SET snapshot_path=? WHERE id=?',(str(Path(target)/relative).replace('\\','/'),sid))
            db.commit()
            db.execute('PRAGMA journal_mode=DELETE')
        manifest={'source_count':len(rows),'target_data':target,'note':'保留事项、反馈、编号、清理日志和资料快照；设备配置、模型密钥及旧运行任务不迁移。'}
        (destination/'migration.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf8')
        return manifest
    except Exception:
        shutil.rmtree(destination)
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--destination',required=True);p.add_argument('--target',default='/var/lib/wechatworkdesk')
    a=p.parse_args();print(json.dumps(prepare(a.source,a.destination,a.target),ensure_ascii=False))
