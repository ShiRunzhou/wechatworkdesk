"""Archive first, remove aged snapshots, retain source tombstones and back-reference clues."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path


class StorageMaintenance:
    def init_storage(self,con):
        columns={r[1] for r in con.execute('PRAGMA table_info(sources)')}
        if 'cleaned_at' not in columns:con.execute("ALTER TABLE sources ADD COLUMN cleaned_at TEXT NOT NULL DEFAULT ''")
        if 'last_seen_at' not in columns:
            con.execute("ALTER TABLE sources ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''")
            con.execute('UPDATE sources SET last_seen_at=created_at')
        con.executescript('''
        CREATE INDEX IF NOT EXISTS import_sources_by_source ON import_sources(source_id);
        CREATE INDEX IF NOT EXISTS matter_evidence_by_source ON matter_evidence(source_id);
        CREATE TABLE IF NOT EXISTS storage_previews(id TEXT PRIMARY KEY,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS storage_deletions(source_id TEXT PRIMARY KEY,path TEXT NOT NULL,planned_at TEXT NOT NULL,
          status TEXT NOT NULL,error TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS storage_log(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,summary_json TEXT NOT NULL);
        ''')

    def _source_protected(self,con,sid):
        if con.execute("SELECT 1 FROM matter_evidence e JOIN matters m ON m.id=e.matter_id WHERE e.source_id=? AND (m.state NOT IN ('completed','irrelevant') OR m.changed=1 OR m.status_unverified=1) LIMIT 1",(sid,)).fetchone():return True
        if con.execute("SELECT 1 FROM import_sources x JOIN run_imports ri ON ri.import_id=x.import_id JOIN runs r ON r.id=ri.run_id WHERE x.source_id=? AND r.state='reading' LIMIT 1",(sid,)).fetchone():return True
        return any(sid in {e['source_id'] for e in json.loads(r[0])} for r in con.execute("SELECT evidence_json FROM backgrounds WHERE state='candidate'"))

    def storage_policy(self,con=None):
        if con is None:
            with self.connect() as db:return self.storage_policy(db)
        row=con.execute("SELECT value FROM memory_settings WHERE key='storage_policy'").fetchone()
        return {'retention_days':90,'auto_enabled':False,**(json.loads(row[0]) if row else {})}

    def save_storage_policy(self,payload):
        from .store import DeskError,packed
        days=payload.get('retention_days',90);enabled=payload.get('auto_enabled',False)
        if type(days)is not int or not 7<=days<=730 or type(enabled)is not bool:raise DeskError('保留天数须为7到730天，自动清理须为开关')
        with self.connect() as con:con.execute("INSERT OR REPLACE INTO memory_settings VALUES('storage_policy',?)",(packed(dict(retention_days=days,auto_enabled=enabled)),))
        return self.storage_policy()

    def _storage_candidates(self,con):
        policy=self.storage_policy(con);cutoff=datetime.now(timezone.utc)-timedelta(days=policy['retention_days'])
        protected={r[0] for r in con.execute("SELECT e.source_id FROM matter_evidence e JOIN matters m ON m.id=e.matter_id WHERE m.state NOT IN ('completed','irrelevant') OR m.changed=1 OR m.status_unverified=1")}
        for row in con.execute("SELECT evidence_json FROM backgrounds WHERE state='candidate'"):
            protected.update(e['source_id'] for e in json.loads(row[0]))
        # Unfinished Agent/API rounds must not lose the materials they were given.
        protected.update(r[0] for r in con.execute("SELECT x.source_id FROM import_sources x JOIN run_imports ri ON ri.import_id=x.import_id JOIN runs r ON r.id=ri.run_id WHERE r.state='reading'"))
        candidates=[];kept=0
        for row in con.execute('SELECT s.*,o.name AS object_name FROM sources s JOIN objects o ON o.id=s.object_id'):
            s=dict(row)
            if s['cleaned_at']:
                pending=con.execute("SELECT status FROM storage_deletions WHERE source_id=? AND status IN ('queued','failed')",(s['id'],)).fetchone()
                if pending and s['id'] not in protected:candidates.append(s)
                elif pending:kept+=1
                continue
            stamp=datetime.fromisoformat(s['last_seen_at'] or s['created_at'])
            if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            if stamp>=cutoff:continue
            if s['id'] in protected:kept+=1;continue
            candidates.append(s)
        return candidates,kept

    def storage_preview(self):
        from .store import uid,now,packed
        with self.connect() as con:
            candidates,kept=self._storage_candidates(con)
            token=uid();snapshot=[dict(id=s['id'],sha256=s['sha256'],last_seen_at=s['last_seen_at'],cleaned_at=s['cleaned_at']) for s in candidates]
            con.execute('INSERT INTO storage_previews VALUES(?,?,?)',(token,packed(snapshot),now()))
            con.execute('DELETE FROM storage_previews WHERE julianday(created_at)<julianday(?)',((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
            logs=[{**dict(r),'summary':json.loads(r['summary_json'])} for r in con.execute('SELECT * FROM storage_log ORDER BY created_at DESC LIMIT 20')]
        return dict(token=token,policy=self.storage_policy(),candidates=candidates,protected=kept,
                    bytes=sum(s['size'] for s in candidates),logs=logs,
                    note='按资料最近登记/更新日期计算；未完成事项、待确认背景和未结束阅读轮次的依据保留。先归档再删除，原文件名、定位及哈希仍可回查外部备份。')

    def storage_clean(self,payload):
        from .store import DeskError,now,packed,uid
        if payload.get('confirmed') is not True:raise DeskError('请确认资料清理')
        token=payload.get('token');ids=payload.get('ids')
        if not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise DeskError('清理选择无效')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            preview=con.execute('SELECT * FROM storage_previews WHERE id=?',(token,)).fetchone()
            if not preview or datetime.fromisoformat(preview['created_at'])<datetime.now(timezone.utc)-timedelta(minutes=30):raise DeskError('清理清单已过期，请重新预览')
            snapshots={s['id']:s for s in json.loads(preview['snapshot_json'])}
            candidates={s['id']:s for s in self._storage_candidates(con)[0]}
            for sid in ids:
                current=candidates.get(sid);old=snapshots.get(sid)
                if not current or not old or any(current[k]!=old[k] for k in ('sha256','last_seen_at','cleaned_at')):raise DeskError('资料已更新或被新事项引用，请重新预览')
            # Commit durable work history before physical removal.
            for row in con.execute("SELECT * FROM matters WHERE state='completed'").fetchall():self._archive_matter(con,dict(row))
        self.scan_feedback()
        stamp=now()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            current={s['id']:s for s in self._storage_candidates(con)[0]}
            for sid in ids:
                if sid not in current or any(current[sid][k]!=snapshots[sid][k] for k in ('sha256','last_seen_at','cleaned_at')):raise DeskError('资料归档期间有更新，请重新预览')
                s=current[sid]
                file=Path(s['snapshot_path']).resolve()
                if not file.is_relative_to(self.root/'sources') or Path(s['snapshot_path']).is_symlink():raise DeskError('待清理路径超出资料快照目录')
                con.execute('UPDATE sources SET cleaned_at=? WHERE id=?',(stamp,sid))
                con.execute('INSERT OR REPLACE INTO storage_deletions VALUES(?,?,?,?,?)',(sid,str(file),stamp,'queued',''))
            con.execute('DELETE FROM storage_previews WHERE id=?',(token,))
        removed=0;failed=[];freed=0
        for sid in ids:
            try:
                with self.connect() as con:
                    con.execute('BEGIN IMMEDIATE');s=self.one(con,'sources',sid)
                    if s['cleaned_at']!=stamp:continue  # a concurrent re-upload restored this source
                    file=Path(s['snapshot_path']).resolve()
                    if not file.is_relative_to(self.root/'sources') or Path(s['snapshot_path']).is_symlink():raise DeskError('路径越界')
                    if self._source_protected(con,sid):
                        if file.is_file():
                            con.execute("UPDATE sources SET cleaned_at='' WHERE id=?",(sid,))
                            con.execute('DELETE FROM storage_deletions WHERE source_id=?',(sid,))
                        continue
                    if file.exists():file.unlink()
                    con.execute("UPDATE storage_deletions SET status='removed',error='' WHERE source_id=?",(sid,))
                removed+=1;freed+=s['size']
            except (OSError,DeskError):
                failed.append(sid)
                with self.connect() as con:con.execute("UPDATE storage_deletions SET status='failed',error='文件暂时无法删除，可再次清理' WHERE source_id=?",(sid,))
        jobs=self._compact_old_jobs()
        # Old published model outputs can contain long quoted details. Preserve facts in memory first.
        with self.connect() as con:
            cutoff=(datetime.now(timezone.utc)-timedelta(days=self.storage_policy(con)['retention_days'])).isoformat()
            for run in con.execute("SELECT * FROM runs WHERE state='published' AND julianday(published_at)<julianday(?)",(cutoff,)).fetchall():
                active=con.execute("SELECT 1 FROM run_matters rm JOIN matters m ON m.id=rm.matter_id WHERE rm.run_id=? AND (m.state NOT IN ('completed','irrelevant') OR m.changed=1 OR m.status_unverified=1)",(run['id'],)).fetchone()
                if not active:con.execute('UPDATE runs SET result_json=? WHERE id=?',(packed(dict(matters=[],backgrounds=[],coverage=[],archived=True,note='原始分析结果已精简，请检索历史事项档案。')),run['id']))
            result=dict(removed=removed,failed=failed,bytes=freed,compacted_jobs=jobs)
            con.execute('INSERT INTO storage_log VALUES(?,?,?)',(uid(),now(),packed(result)))
        return result

    def _compact_old_jobs(self):
        from .flow_config import atomic_json
        cutoff=datetime.now(timezone.utc)-timedelta(days=self.storage_policy()['retention_days']);count=0
        jobs=self.root/'flow/jobs'
        if not jobs.is_dir() or jobs.is_symlink() or jobs.is_junction() or not jobs.resolve().is_relative_to(self.root):return 0
        for folder in jobs.iterdir():
            if folder.is_symlink() or folder.is_junction() or not folder.is_dir() or folder.resolve().parent!=jobs.resolve() or len(folder.name)!=32 or any(c not in '0123456789abcdef' for c in folder.name):continue
            state=folder/'state.json'
            try:
                j=json.loads(state.read_text('utf8'))
                if j.get('status') not in ('completed','cancelled') or j.get('materials_cleaned_at'):continue
                if datetime.fromtimestamp(state.stat().st_mtime,timezone.utc)>=cutoff:continue
                with self.connect() as con:
                    active=con.execute("SELECT 1 FROM run_matters rm JOIN matters m ON m.id=rm.matter_id WHERE rm.run_id=? AND (m.state NOT IN ('completed','irrelevant') OR m.changed=1 OR m.status_unverified=1)",(j.get('run_id'),)).fetchone()
                if active:continue
                files=list(folder.rglob('*'))
                if any(p.is_symlink() or p.is_junction() for p in files):continue
                for file in files:
                    if file.is_file() and file!=state:file.unlink()
                j.pop('config',None);j.pop('ledger',None);j['materials_cleaned_at']=datetime.now(timezone.utc).isoformat();j['phase']='历史任务资料已清理；处理结果已保留'
                atomic_json(state,j);count+=1
            except (OSError,ValueError):continue
        return count

    def storage_auto(self):
        from .store import packed
        day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        with self.connect() as con:
            if not self.storage_policy(con)['auto_enabled']:return None
            last=con.execute("SELECT value FROM memory_settings WHERE key='storage_auto_day'").fetchone()
            if last and last[0]==day:return None
        preview=self.storage_preview()
        result=self.storage_clean(dict(token=preview['token'],ids=[s['id'] for s in preview['candidates']],confirmed=True))
        with self.connect() as con:con.execute("INSERT OR REPLACE INTO memory_settings VALUES('storage_auto_day',?)",(day,))
        return result
