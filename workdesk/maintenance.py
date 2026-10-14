"""User-confirmed cleanup of old completed matters; retain a minimal ledger."""
from datetime import datetime, timedelta, timezone
import json

class Maintenance:
    def init_maintenance(self, con):
        if 'completed_at' not in {r[1] for r in con.execute('PRAGMA table_info(matters)')}:
            con.execute("ALTER TABLE matters ADD COLUMN completed_at TEXT NOT NULL DEFAULT ''")
        con.execute('''CREATE TABLE IF NOT EXISTS cleanup_log(
            matter_id TEXT PRIMARY KEY,object_id TEXT NOT NULL,dedup_key TEXT NOT NULL,
            reference_code TEXT NOT NULL,title TEXT NOT NULL,object_name TEXT NOT NULL,
            started_at TEXT NOT NULL,completed_at TEXT NOT NULL,cleaned_at TEXT NOT NULL,
            UNIQUE(object_id,dedup_key))''')
        for m in con.execute("SELECT id FROM matters WHERE state='completed' AND completed_at='' ").fetchall():
            completed=''
            for f in con.execute("SELECT before_json,after_json,created_at FROM feedback WHERE entity_type='matter' AND entity_id=? ORDER BY rowid",(m['id'],)):
                before,after=json.loads(f['before_json']),json.loads(f['after_json'])
                if after.get('state')=='completed' and before.get('state')!='completed':completed=f['created_at']
                elif after.get('state')!='completed':completed=''
            if completed:con.execute('UPDATE matters SET completed_at=? WHERE id=?',(completed,m['id']))

    def cleanup_preview(self):
        cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        with self.connect() as con:
            candidates=[dict(r) for r in con.execute('''SELECT m.id,m.version,m.reference_code,m.title,
                m.notice_at AS started_at,m.completed_at,o.name AS object_name
                FROM matters m JOIN objects o ON o.id=m.object_id
                WHERE m.state='completed' AND m.changed=0 AND m.status_unverified=0 AND m.completed_at!='' AND julianday(m.completed_at)<julianday(?)
                ORDER BY m.completed_at''',(cutoff,))]
            logs=[dict(r) for r in con.execute('SELECT * FROM cleanup_log ORDER BY cleaned_at DESC')]
        return {'candidates':candidates,'logs':logs}

    def cleanup_confirm(self,payload):
        from .store import DeskError, now
        items=payload.get('items')
        if payload.get('confirmed') is not True or not isinstance(items,list) or not items:
            raise DeskError('请勾选事项并确认清理')
        if len(items)>1000:raise DeskError('一次最多清理1000项')
        cutoff=datetime.now(timezone.utc)-timedelta(days=30)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            rows=[];seen=set()
            for item in items:
                if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] in seen:
                    raise DeskError('清理选择格式错误或重复')
                seen.add(item['id']);m=self.one(con,'matters',item['id'])
                if type(item.get('version')) is not int or m['version']!=item['version']:
                    raise DeskError('事项已更新，请重新打开清理清单')
                if m['state']!='completed' or m['changed'] or m['status_unverified'] or not m['completed_at'] or datetime.fromisoformat(m['completed_at'])>=cutoff:
                    raise DeskError('只能清理完成超过30天的事项')
                rows.append(m)
            stamp=now()
            for m in rows:
                self._archive_matter(con,m)
                obj=self.one(con,'objects',m['object_id'])
                con.execute('INSERT INTO cleanup_log VALUES(?,?,?,?,?,?,?,?,?)',(m['id'],m['object_id'],m['dedup_key'],m['reference_code'],m['title'],obj['name'],m['notice_at'] or m['created_at'],m['completed_at'],stamp))
                for table in ('matter_evidence','run_matters','handoffs'):
                    con.execute(f'DELETE FROM {table} WHERE matter_id=?',(m['id'],))
                con.execute("DELETE FROM feedback WHERE entity_type='matter' AND entity_id=?",(m['id'],))
                con.execute('DELETE FROM matters WHERE id=?',(m['id'],))
        self.export_memory()
        return {'cleaned':len(rows),'cleaned_at':stamp}
