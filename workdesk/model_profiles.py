"""Named model configurations, with per-profile encrypted credentials."""
import json, uuid
from .store import DeskError

class Profiles:
    def _profiles(self):
        from .flow_config import atomic_json, DEFAULTS
        path=self.path.parent/'model-profiles.json'
        keys=set(DEFAULTS)-{'default_goal','material_roots','last_selection'}
        if not path.exists():
            c=self.internal();pid=uuid.uuid4().hex
            data={'active':pid,'profiles':[{'id':pid,'name':'默认接口',**{k:c[k] for k in keys},'encrypted_key':c.get('encrypted_key','')}]}
            atomic_json(path,data)
        return path,json.loads(path.read_text('utf8')),keys

    def public(self):
        c=self._public_current()
        with self.lock:
            _,data,_=self._profiles()
            c['active_profile']=data['active']
            c['profiles']=[{'id':p['id'],'name':p['name'],'model':p['model'],'base_url':p['base_url'],'key_saved':bool(p.get('encrypted_key'))} for p in data['profiles']]
            c['profile_name']=next(p['name'] for p in data['profiles'] if p['id']==data['active'])
        return c

    def save(self,payload):
        from .flow_config import atomic_json, DEFAULTS
        with self.lock:
            path,data,keys=self._profiles()
            action=payload.get('profile_action')
            if action in ('select','create','delete'):
                if action=='create':
                    if len(data['profiles'])>=30:raise DeskError('最多保存30个接口')
                    name=payload.get('profile_name','新接口')
                    if not isinstance(name,str) or not name.strip() or len(name)>100:raise DeskError('配置名称须为1—100字')
                    pid=uuid.uuid4().hex
                    data['profiles'].append({'id':pid,'name':name.strip(),**{k:DEFAULTS[k] for k in keys}})
                    data['active']=pid
                else:
                    pid=payload.get('profile_id')
                    if not any(p['id']==pid for p in data['profiles']):raise DeskError('接口配置不存在')
                    if action=='delete':
                        if len(data['profiles'])==1:raise DeskError('请至少保留一套接口配置')
                        data['profiles']=[p for p in data['profiles'] if p['id']!=pid]
                        if data['active']==pid:data['active']=data['profiles'][0]['id']
                    else:data['active']=pid
                selected=next(p for p in data['profiles'] if p['id']==data['active'])
                current=self.internal()
                current.update({k:selected.get(k,DEFAULTS[k]) for k in keys});current.pop('encrypted_key',None)
                if selected.get('encrypted_key'):current['encrypted_key']=selected['encrypted_key']
                atomic_json(self.path,current);atomic_json(path,data)
            elif action is not None:raise DeskError('不支持的接口操作')
            else:
                selected=next(p for p in data['profiles'] if p['id']==data['active'])
                name=payload.get('profile_name',selected['name'])
                if not isinstance(name,str) or not name.strip() or len(name)>100:raise DeskError('配置名称须为1—100字')
                self._save_current(payload)
                current=self.internal();selected.update({k:current[k] for k in keys})
                selected['name']=name.strip();selected['encrypted_key']=current.get('encrypted_key','')
                atomic_json(path,data)
            return self.public()
