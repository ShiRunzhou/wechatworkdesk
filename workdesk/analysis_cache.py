"""Reuse only published analysis; message content and human instructions invalidate it."""
import hashlib
import json
from .flow_config import atomic_json

VERSION=1

def signature(store,oid,config,supplement):
    with store.connect() as con:
        memory=store._memory_context(con,oid)
        backgrounds=[dict(r) for r in con.execute("SELECT summary,role,uncertainties FROM backgrounds WHERE object_id=? AND state='confirmed' ORDER BY confirmed_at DESC,rowid DESC LIMIT 1",(oid,))]
        # Human edits, including explicit ownership/state judgments, are semantic input.
        feedback=[dict(r) for r in con.execute("SELECT f.id,f.action,f.note FROM feedback f JOIN matters m ON m.id=f.entity_id WHERE f.entity_type='matter' AND m.object_id=? ORDER BY f.id",(oid,))]
        bg_sources=[tuple(r) for r in con.execute("SELECT relative_path,sha256 FROM sources WHERE object_id=? AND relative_path LIKE 'background/%' AND cleaned_at='' ORDER BY relative_path,sha256",(oid,))]
    semantics={k:config[k] for k in ('default_goal','base_url','model','vision','extra_parameters')}
    value=[VERSION,semantics,supplement,backgrounds,bg_sources,feedback,{k:memory[k] for k in ('profile','rules','feedback')}]
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def load(root):
    path=root/'analysis-cache.json'
    return json.loads(path.read_text('utf8')) if path.exists() else {}

def published(root,candidates,run_id):
    cache=load(root)
    for oid,candidate in candidates.items():
        key=oid+':'+candidate['signature'];old=cache.get(key,{})
        cache[key]={'signature':candidate['signature'],'run_id':run_id,'warnings':list(dict.fromkeys(old.get('warnings',[])+candidate.get('warnings',[]))),
                    'messages':{**old.get('messages',{}),**candidate.get('messages',{})},
                    'sources':{**old.get('sources',{}),**candidate.get('sources',{})}}
    atomic_json(root/'analysis-cache.json',cache)
