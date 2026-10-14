"""Durable work memory, explicit rules and reviewable behavioural suggestions."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import uuid


def safe_text(text):
    # Archive useful work facts, not labelled credentials or URL user-info.
    text = re.sub(r'(?im)(密码|口令|api[_ -]?key|token|secret)\s*[:：=]\s*[^\n]+', r'\1：[凭据省略]', str(text))
    return re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[凭据省略]@', text)


class WorkMemory:
    def init_memory(self, con):
        con.executescript('''
        CREATE TABLE IF NOT EXISTS work_rules(
          id TEXT PRIMARY KEY,object_id TEXT NOT NULL,content TEXT NOT NULL,kind TEXT NOT NULL,
          match_terms TEXT NOT NULL,exceptions TEXT NOT NULL,state TEXT NOT NULL,origin TEXT NOT NULL,
          basis_json TEXT NOT NULL,signature TEXT NOT NULL UNIQUE,version INTEGER NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_history(
          matter_id TEXT PRIMARY KEY,object_id TEXT NOT NULL,reference_code TEXT NOT NULL,title TEXT NOT NULL,
          started_at TEXT NOT NULL,completed_at TEXT NOT NULL,current_state TEXT NOT NULL,
          summary TEXT NOT NULL,resolution TEXT NOT NULL,evidence_json TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback_memory(
          id TEXT PRIMARY KEY,object_id TEXT NOT NULL,matter_id TEXT NOT NULL,reference_code TEXT NOT NULL,
          title TEXT NOT NULL,state TEXT NOT NULL,note TEXT NOT NULL,category TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_audit(
          id TEXT PRIMARY KEY,entity_id TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS filtered_items(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,object_id TEXT NOT NULL,rule_id TEXT NOT NULL,
          title TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL);
        ''')
        if 'dedup_key' not in {r[1] for r in con.execute('PRAGMA table_info(work_history)')}:
            con.execute("ALTER TABLE work_history ADD COLUMN dedup_key TEXT NOT NULL DEFAULT ''")
        columns = {r[1] for r in con.execute('PRAGMA table_info(matters)')}
        if 'memory_category' not in columns:con.execute("ALTER TABLE matters ADD COLUMN memory_category TEXT NOT NULL DEFAULT ''")
        if 'memory_note' not in columns:con.execute("ALTER TABLE matters ADD COLUMN memory_note TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _rule_fields(item):
        from .store import DeskError, string
        content = string(item.get('content', ''), '规则内容', 2000, True)
        exceptions = string(item.get('exceptions', ''), '例外', 2000)
        kind = item.get('kind', 'advice')
        if kind not in ('advice', 'exclude'):raise DeskError('规则用途须为判断参考或无需列出')
        terms = item.get('match_terms', [])
        if not isinstance(terms, list) or len(terms)>12 or any(not isinstance(t,str) or not t.strip() or len(t)>100 for t in terms):
            raise DeskError('适用关键词最多12项，每项1到100字')
        return content,kind,list(dict.fromkeys(t.strip() for t in terms)),exceptions

    def _add_rule(self, con, oid, item, origin, basis, active=False, signature=None):
        from .store import uid, now, packed, DeskError
        content, kind, terms, exceptions = self._rule_fields(item)
        if oid:self.one(con,'objects',oid)
        if active and kind=='exclude' and not terms:raise DeskError('无需列出的规则须填写适用关键词，明确范围')
        signature = signature or hashlib.sha256(packed([oid,origin,content,kind]).encode()).hexdigest()
        old = con.execute('SELECT * FROM work_rules WHERE signature=?',(signature,)).fetchone()
        if old:
            if active:
                con.execute("UPDATE work_rules SET state='active',kind=?,match_terms=?,exceptions=?,basis_json=?,version=version+1,updated_at=? WHERE id=?",(kind,packed(terms),safe_text(exceptions),packed(basis),now(),old['id']))
            elif old['state']=='candidate' and old['basis_json']!=packed(basis):
                con.execute('UPDATE work_rules SET basis_json=?,version=version+1,updated_at=? WHERE id=?',(packed(basis),now(),old['id']))
            return old['id']
        rid, stamp = uid(), now()
        con.execute('INSERT INTO work_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (rid,oid,safe_text(content),kind,packed(terms),safe_text(exceptions),'active' if active else 'candidate',
                     origin,packed(basis),signature,1,stamp,stamp))
        return rid

    def _remember_feedback(self, con, feedback_id, matter, note, category, created_at, explicit_rule=None):
        from .store import DeskError
        remembered=note+('\n处理交代：'+matter.get('memory_note','') if matter.get('memory_note') else '')
        con.execute('INSERT OR IGNORE INTO feedback_memory VALUES(?,?,?,?,?,?,?,?,?)',
                    (feedback_id,matter['object_id'],matter['id'],matter['reference_code'],matter['title'],
                     matter['state'],safe_text(remembered),category,created_at))
        basis=[dict(feedback_id=feedback_id,reference_code=matter['reference_code'],title=matter['title'],note=safe_text(remembered),state=matter['state'])]
        if explicit_rule:
            if not isinstance(explicit_rule,dict) or explicit_rule.get('confirmed') is not True:
                raise DeskError('请明确确认长期规则')
            self._add_rule(con,matter['object_id'],explicit_rule,'explicit',basis,active=True)
        elif remembered and re.search(r'以后|今后|此后|通常|(?:无需|不需要|不用).{0,12}(?:转达|转发)',remembered):
            self._add_rule(con,matter['object_id'],dict(content=remembered[:2000],kind='advice',match_terms=[category] if category else [],exceptions='明确要求本人承担、参加、组织或转达的内容需单独核查。'),'feedback',basis)
        if category:
            rows=[dict(r) for r in con.execute('''SELECT * FROM feedback_memory WHERE object_id=? AND category=? AND state='irrelevant'
                 ORDER BY created_at DESC''',(matter['object_id'],category))]
            latest={}
            for r in rows:
                if r['matter_id'] in latest:continue
                current=con.execute('SELECT state FROM matters WHERE id=?',(r['matter_id'],)).fetchone()
                if current and current['state']=='irrelevant':latest[r['matter_id']]=r
            if len(latest)>=3:
                rule=dict(content=f'多次将“{category}”标记为与我无关，建议核对是否通常无需列出此类通知。',kind='exclude',match_terms=[category],exceptions='明确要求本人承担、参加、组织或转达的内容仍列出；与我无关不代表不重要。')
                self._add_rule(con,matter['object_id'],rule,'behavior',list(latest.values()),signature='behavior:'+matter['object_id']+':'+category)
        if matter['state']=='completed':self._archive_matter(con,matter)
        else:
            con.execute('UPDATE work_history SET current_state=?,updated_at=? WHERE matter_id=?',(matter['state'],created_at,matter['id']))
            if matter['state'] not in ('completed','irrelevant'):
                for ref in self.evidence(con,matter['id']):
                    row=con.execute("SELECT s.* FROM sources s JOIN storage_deletions d ON d.source_id=s.id WHERE s.id=? AND d.status IN ('queued','failed')",(ref['source_id'],)).fetchone()
                    if row:
                        file=Path(row['snapshot_path'])
                        if not file.is_symlink() and file.resolve().is_relative_to(self.root/'sources') and file.is_file():
                            con.execute("UPDATE sources SET cleaned_at='' WHERE id=?",(row['id'],))
                            con.execute('DELETE FROM storage_deletions WHERE source_id=?',(row['id'],))

    @staticmethod
    def infer_category(title):
        for term in ('讲座报名','活动报名','志愿招募','培训报名','个人评奖','项目申报','竞赛报名','数据填报'):
            if term in title:return term
        return ''

    def _archive_matter(self, con, matter):
        from .store import packed, now
        if matter['changed'] and con.execute('SELECT 1 FROM work_history WHERE matter_id=?',(matter['id'],)).fetchone():
            con.execute("UPDATE work_history SET current_state='completed_pending_review' WHERE matter_id=?",(matter['id'],))
            return
        refs=self.evidence(con,matter['id'])
        for ref in refs:
            source=self.one(con,'sources',ref['source_id'])
            ref['original_path']=con.execute('SELECT original_path FROM imports i JOIN import_sources x ON x.import_id=i.id WHERE x.source_id=? ORDER BY i.created_at LIMIT 1',(source['id'],)).fetchone()[0]
            ref['notice']='资料可能已从工作台清理；可凭原文件名、消息定位和哈希回查外部备份'
        feedback=[dict(r) for r in con.execute('SELECT id,note,created_at FROM feedback WHERE entity_type=\'matter\' AND entity_id=? ORDER BY created_at',(matter['id'],))]
        notes='\n'.join('['+f['created_at']+'] '+safe_text(f['note']) for f in reversed(feedback) if f['note'])
        fallback=('用户标记为已完成；未单独填写完成交代。以下为已有反馈（最新在前）：\n'+(notes or safe_text(matter['human_note']))) if notes or matter['human_note'] else '用户标记为已完成；尚未填写具体处理结果及完成依据。'
        resolution=safe_text(matter.get('memory_note','') or fallback)[:6000]
        summary=safe_text('事项要求：'+matter['detail']+'\n本人责任（当时记录）：'+(matter['owner'] or '未明确')+'\n原期限：'+(matter['deadline'] or '未明确'))[:6000]
        con.execute('''INSERT INTO work_history(matter_id,object_id,reference_code,title,started_at,completed_at,current_state,summary,resolution,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(matter_id) DO UPDATE SET
          title=excluded.title,completed_at=excluded.completed_at,current_state=excluded.current_state,
          summary=excluded.summary,resolution=excluded.resolution,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at
          WHERE work_history.summary!=excluded.summary OR work_history.resolution!=excluded.resolution OR
          work_history.completed_at!=excluded.completed_at OR work_history.current_state!=excluded.current_state OR work_history.evidence_json!=excluded.evidence_json''',
          (matter['id'],matter['object_id'],matter['reference_code'],matter['title'],matter['notice_at'] or matter['created_at'],
           matter['completed_at'],'completed',summary,resolution,packed(refs),now()))
        con.execute('UPDATE work_history SET dedup_key=? WHERE matter_id=?',(matter['dedup_key'],matter['id']))
        if matter['changed']:con.execute("UPDATE work_history SET current_state='completed_pending_review' WHERE matter_id=?",(matter['id'],))

    def _memory_context(self, con, oid):
        profile=con.execute("SELECT value FROM memory_settings WHERE key='profile'").fetchone()
        rules=[self._public_rule(dict(r)) for r in con.execute("SELECT * FROM work_rules WHERE state='active' AND (object_id=? OR object_id='') ORDER BY updated_at DESC",(oid,))]
        rules=[{k:r[k] for k in ('id','object_id','content','kind','match_terms','exceptions','version')} for r in rules]
        history=[dict(r) for r in con.execute('SELECT matter_id,dedup_key,reference_code,title,started_at,completed_at,current_state,summary,resolution FROM work_history WHERE object_id=? ORDER BY completed_at DESC,updated_at DESC LIMIT 5',(oid,))]
        for h in history:
            h['summary']=h['summary'][:800];h['resolution']=h['resolution'][:1200]
        feedback=[dict(r) for r in con.execute('SELECT * FROM feedback_memory WHERE object_id=? ORDER BY created_at DESC LIMIT 12',(oid,))]
        for f in feedback:f['note']=f['note'][:500]
        return dict(profile=profile[0] if profile else '',rules=rules,history=history,feedback=feedback,
                    note='仅已生效规则用于判断；历史完成后重新打开的事项按当前状态处理。可检索更多历史档案。')

    @staticmethod
    def _public_rule(row):
        row['match_terms']=json.loads(row['match_terms']);row['basis']=json.loads(row.pop('basis_json'))
        return row

    def memory_state(self):
        with self.connect() as con:
            profile=con.execute("SELECT value FROM memory_settings WHERE key='profile'").fetchone()
            return dict(profile=profile[0] if profile else '',
                rules=[self._public_rule(dict(r)) for r in con.execute('SELECT * FROM work_rules ORDER BY updated_at DESC')],
                history=[dict(r) for r in con.execute('SELECT * FROM work_history ORDER BY updated_at DESC')],
                filtered=[{**dict(r),'evidence':json.loads(r['evidence_json'])} for r in con.execute('SELECT * FROM filtered_items ORDER BY created_at DESC LIMIT 100')])

    def save_memory(self,payload):
        from .store import string, now, packed, uid, DeskError
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if 'profile' in payload:
                value=safe_text(string(payload['profile'],'工作概况',6000))
                before=con.execute("SELECT value FROM memory_settings WHERE key='profile'").fetchone()
                con.execute("INSERT OR REPLACE INTO memory_settings VALUES('profile',?)",(value,))
                con.execute('INSERT INTO memory_audit VALUES(?,?,?,?,?)',(uid(),'profile',before[0] if before else '',value,now()))
            elif payload.get('id'):
                row=con.execute('SELECT * FROM work_rules WHERE id=?',(payload['id'],)).fetchone()
                if row is None or payload.get('version')!=row['version']:raise DeskError('规则已更新，请刷新')
                state=payload.get('state','candidate')
                if state not in ('candidate','active','disabled','rejected'):raise DeskError('规则状态无效')
                content,kind,terms,exceptions=self._rule_fields(payload)
                if state=='active' and kind=='exclude' and not terms:raise DeskError('无需列出的规则须填写适用关键词')
                con.execute('UPDATE work_rules SET content=?,kind=?,match_terms=?,exceptions=?,state=?,version=version+1,updated_at=? WHERE id=?',
                    (safe_text(content),kind,packed(terms),safe_text(exceptions),state,now(),row['id']))
                con.execute('INSERT INTO memory_audit VALUES(?,?,?,?,?)',(uid(),row['id'],packed(dict(row)),packed(payload),now()))
            else:
                self._add_rule(con,string(payload.get('object_id',''),'适用对象',32),payload,'explicit',[],active=payload.get('state')=='active')
        self.export_memory()
        return self.memory_state()

    def scan_feedback(self):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            for f in con.execute("SELECT * FROM feedback WHERE entity_type='matter' ORDER BY created_at").fetchall():
                if con.execute('SELECT 1 FROM feedback_memory WHERE id=?',(f['id'],)).fetchone():continue
                after=json.loads(f['after_json'])
                if not con.execute('SELECT 1 FROM matters WHERE id=?',(f['entity_id'],)).fetchone():continue
                current=self.one(con,'matters',f['entity_id'])
                category=current['memory_category'] or self.infer_category(current['title'])
                self._remember_feedback(con,f['id'],{**current,**after,'reference_code':current['reference_code']},f['note'],category,f['created_at'])
            for row in con.execute('SELECT * FROM matters').fetchall():
                if row['state']=='completed':self._archive_matter(con,dict(row))
                else:con.execute('UPDATE work_history SET current_state=? WHERE matter_id=?',(row['state'],row['id']))
            for row in con.execute('SELECT * FROM cleanup_log').fetchall():
                con.execute('''INSERT OR IGNORE INTO work_history(matter_id,object_id,reference_code,title,started_at,completed_at,current_state,summary,resolution,evidence_json,updated_at,dedup_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(row['matter_id'],row['object_id'],row['reference_code'],row['title'],row['started_at'],row['completed_at'],'completed',
                    '旧版清理日志保留的事项；原详细记录已移除。','旧日志记录为完成；具体处理交代未保留，可到外部备份核查。','[]',row['cleaned_at'],row['dedup_key']))
        self.export_memory()
        return self.memory_state()

    def search_memory(self,query,oid='',limit=8):
        from .store import string
        query=string(query,'记忆检索词',200,True)
        terms=[q for q in re.split(r'[\s，,；;]+',query) if q]
        with self.connect() as con:
            rows=[dict(r) for r in con.execute('SELECT * FROM work_history WHERE object_id=? OR ?=\'\'',(oid,oid))]
        scored=[]
        for r in rows:
            body=' '.join(str(r[k]) for k in ('title','summary','resolution','reference_code'))
            score=sum(body.lower().count(t.lower()) for t in terms)
            if score:scored.append((score,r))
        hits=[r for _,r in sorted(scored,key=lambda item:(item[0],item[1]['updated_at']),reverse=True)[:limit]]
        for r in hits:
            r['evidence']=json.loads(r.pop('evidence_json'));r['summary']=r['summary'][:2000];r['resolution']=r['resolution'][:3000]
        return hits

    def export_memory(self):
        """Markdown is a readable projection; SQLite is the canonical memory."""
        state=self.memory_state();folder=self.root/'memory';folder.mkdir(exist_ok=True)
        if folder.is_symlink() or folder.is_junction() or not folder.resolve().is_relative_to(self.root):
            from .store import DeskError
            raise DeskError('记忆文件目录不能链接到工作台外部')
        with self.connect() as con:names={r['id']:r['name'] for r in con.execute('SELECT id,name FROM objects')}
        years={}
        for h in state['history']:years.setdefault((h['started_at'] or h['updated_at'])[:4],[]).append(h)
        files={'working-memory.md':'# 工作概况与已确认规则\n\n'+(state['profile'] or '尚未填写统一工作概况。')+'\n\n'+
            '\n\n'.join(f'## {r["content"]}\n适用对象：{names.get(r["object_id"],"全部对象")}\n用途：{r["kind"]}\n例外：{r["exceptions"]}\n规则编号：{r["id"]}' for r in state['rules'] if r['state']=='active')}
        for year,items in years.items():
            files['history-'+year+'.md']='# '+year+' 历史事项档案\n\n'+'\n\n'.join(
                f'## {h["reference_code"]} {h["title"]}\n对象：{names.get(h["object_id"],h["object_id"])}\n起始：{h["started_at"]}\n完成：{h["completed_at"]}\n当前状态：{h["current_state"]}\n\n{h["summary"]}\n\n处理交代：{h["resolution"]}\n\n回查线索：\n'+
                '\n'.join(f'- {e["relative_path"]} — {e["locator"]}；SHA256 {e["sha256"]}' for e in json.loads(h['evidence_json'])) for h in items)
        for name,text in files.items():
            temp=folder/('.write-'+uuid.uuid4().hex);temp.write_text(text,'utf8');temp.replace(folder/name)
        return {'files':[str(folder/name) for name in files]}

    def _submit_memory(self,con,rid,payload,allowed,coverage):
        from .store import DeskError,string,packed,uid,now
        suggestions=payload.get('rule_suggestions',[]);filtered=payload.get('filtered_items',[])
        if not isinstance(suggestions,list) or len(suggestions)>20 or not isinstance(filtered,list) or len(filtered)>200:raise DeskError('规则建议或过滤清单格式无效')
        oids={s['object_id'] for s in allowed.values()}
        for item in suggestions:
            oid=item.get('object_id') if isinstance(item,dict) else None
            if oid not in oids:raise DeskError('规则建议对象不属于本轮')
            ids=item.get('feedback_ids',[])
            if not isinstance(ids,list) or not ids or len(ids)>30:raise DeskError('规则建议须引用明确的人工反馈')
            basis=[]
            for fid in ids:
                if not isinstance(fid,str):raise DeskError('反馈编号须为文本')
                row=con.execute('SELECT * FROM feedback_memory WHERE id=? AND object_id=?',(fid,oid)).fetchone()
                if row is None:raise DeskError('规则建议引用了不存在或其他对象的反馈')
                basis.append(dict(row))
            self._add_rule(con,oid,item,'model',basis)
        for item in filtered:
            if not isinstance(item,dict) or item.get('object_id') not in oids:raise DeskError('过滤清单对象错误')
            rule=con.execute("SELECT * FROM work_rules WHERE id=? AND state='active' AND kind='exclude'",(item.get('rule_id'),)).fetchone()
            if rule is None or rule['object_id'] not in ('',item['object_id']):raise DeskError('仅已生效且适用的无需列出规则可以过滤')
            if item.get('rule_version')!=rule['version']:raise DeskError('过滤所依据的规则版本已改变，请重新分析')
            evidence=self.validate_evidence(item.get('evidence'),item['object_id'],allowed,coverage)
            con.execute('INSERT INTO filtered_items VALUES(?,?,?,?,?,?,?,?)',(uid(),rid,item['object_id'],rule['id'],
                        string(item.get('title',''),'过滤内容主题',2000,True),string(item.get('reason',''),'过滤原因',2000,True),packed(evidence),now()))
