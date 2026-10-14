"""Replaceable Chat Completions adapter; no native tools are required."""
import json
import re
import socket
import ssl
from urllib import request,error,parse
from .store import DeskError

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class ModelAPI:
    def __init__(self,config,key=''):
        self.config=config;self.key=key
        self.base=config['base_url'].rstrip('/')
        if self.base.endswith('/chat/completions'):self.base=self.base[:-17]
        self.local=parse.urlsplit(self.base).hostname in ('127.0.0.1','localhost','::1')
    def _request(self,path,payload=None,proxy=False):
        headers={'Content-Type':'application/json'}
        if self.key:headers['Authorization']='Bearer '+self.key
        req=request.Request(self.base+path,data=json.dumps(payload,ensure_ascii=False).encode('utf8') if payload is not None else None,headers=headers)
        opener=request.build_opener(request.ProxyHandler({'http':'http://127.0.0.1:1080','https':'http://127.0.0.1:1080'} if proxy else {}),NoRedirect())
        try:
            with opener.open(req,timeout=self.config['timeout']) as response:
                b=response.read(8*1024**2+1)
            if len(b)>8*1024**2:raise DeskError('模型返回过大')
            value=json.loads(b)
            if not isinstance(value,dict):raise DeskError('模型服务响应须为JSON对象')
            return value
        except error.HTTPError as e:
            raise DeskError(f'模型服务返回 HTTP {e.code}；请核对地址、模型和参数。本轮材料未提交到事项列表。') from None
        except (error.URLError,TimeoutError,ConnectionError,ssl.SSLError) as e:
            reason=getattr(e,'reason',e)
            if not proxy and not self.local:
                if payload is None or isinstance(reason,(socket.gaierror,ConnectionRefusedError,ssl.SSLError)):
                    return self._request(path,payload,True)
                # A POST may already have arrived. Probe only; do not replay a paid request.
                try:self._request('/models',None,True)
                except DeskError:pass
            raise DeskError('模型连接失败或超时；未自动重复可能已发送的请求。可稍后重试此任务。') from None
        except (json.JSONDecodeError,UnicodeError):raise DeskError('模型服务没有返回有效 JSON 响应') from None
    def test(self):
        if not self.base:raise DeskError('请先填写 API 地址')
        obj=self._request('/models')
        if not isinstance(obj.get('data'),list):raise DeskError('地址未返回兼容的模型列表，请核对API基础地址')
        return {'connected':True,'models':[str(m['id']) for m in obj.get('data',[]) if isinstance(m,dict) and 'id' in m]}
    def complete(self,messages):
        if not self.base or not self.config['model']:raise DeskError('请先保存 API 地址与模型名称，或选择 Agent 模式')
        payload=dict(model=self.config['model'],messages=messages,stream=False,
                     temperature=self.config['temperature'])
        payload[self.config['output_parameter']]=self.config['max_tokens']
        if self.config['json_mode']:payload['response_format']={'type':'json_object'}
        payload.update(self.config.get('extra_parameters',{}))
        obj=self._request('/chat/completions',payload)
        try:
            choice=obj['choices'][0]
            if choice.get('finish_reason')=='length':raise DeskError('模型输出被截断；请增大输出长度或减小每段材料后新建任务')
            content=choice['message']['content']
            if not isinstance(content,str):raise KeyError()
            return parse_result(content),obj.get('usage',{})
        except (KeyError,IndexError,TypeError):raise DeskError('模型响应缺少可用文字；请核对兼容接口或思考输出设置') from None

def parse_result(text):
    text=re.sub(r'<think>.*?</think>','',text,flags=re.S).strip()
    if text.startswith('```'):text=re.sub(r'^```(?:json)?\s*|\s*```$','',text).strip()
    try:value=json.loads(text)
    except json.JSONDecodeError:raise DeskError('模型结果不是有效 JSON；本轮未发布') from None
    if not isinstance(value,dict):raise DeskError('模型结果须为 JSON 对象')
    return value
