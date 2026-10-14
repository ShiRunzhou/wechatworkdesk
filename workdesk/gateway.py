"""Same-origin desktop entrance to the canonical website, no local matter copy."""
import base64
from urllib import request,error,parse
from .model_api import NoRedirect


def proxy(handler,assistant,body=None):
    path=handler.path
    if not path.startswith('/') or path.startswith('//') or parse.urlsplit(path).netloc:
        return handler.send(400,{'error':'请求路径无效'})
    config=dict(assistant.config);password=assistant.password
    header=handler.headers.get('Authorization','')
    if header.startswith('Basic '):
        try:
            user,password=base64.b64decode(header[6:],validate=True).decode('utf8').split(':',1)
            config['username']=user
        except (ValueError,UnicodeError):return handler.send(401,{'error':'请使用网站账号登录'})
    from .assistant import Remote
    remote=Remote(config,password)
    headers=dict(remote.headers)
    if body is not None:headers['Content-Type']=handler.headers.get('Content-Type','application/json')
    req=request.Request(remote.base+path,data=body,headers=headers,method=handler.command)
    opener=request.build_opener(request.ProxyHandler({}),NoRedirect())
    try:
        response=opener.open(req,timeout=180)
    except error.HTTPError as exc:response=exc
    except (error.URLError,TimeoutError,ConnectionError):
        return handler.send(502,{'error':'网站暂时无法连接，本机已导出资料保留，请稍后重试'})
    with response:
        if response.status<400 and header.startswith('Basic '):
            with assistant.lock:assistant.password=password
        handler.send_response(response.status)
        for name in ('Content-Type','Content-Length','Content-Disposition','WWW-Authenticate','Content-Security-Policy','X-Content-Type-Options','Referrer-Policy','Cache-Control'):
            value=response.headers.get(name)
            if value:handler.send_header(name,value)
        handler.end_headers()
        while block:=response.read(256*1024):handler.wfile.write(block)
