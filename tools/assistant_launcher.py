"""Start the device assistant without replacing another port owner's process."""
import json
import subprocess
import sys
import time
import webbrowser
import os
from pathlib import Path
from urllib import request

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/assistant'
URL='http://127.0.0.1:8771'

def health():
    try:
        with request.build_opener(request.ProxyHandler({})).open(URL+'/api/health',timeout=1) as r:d=json.load(r)
        return d.get('assistant') is True and Path(d.get('root','')).resolve()==DATA.resolve()
    except (OSError,ValueError):return False

def main():
    DATA.mkdir(parents=True,exist_ok=True)
    # Start WeFlow in the background if its local API is not listening.
    sys.path.insert(0,str(ROOT))
    from workdesk.device_paths import load_paths
    from types import SimpleNamespace
    config=json.loads((DATA/'settings.json').read_text('utf8')) if (DATA/'settings.json').exists() else {}
    paths=load_paths(SimpleNamespace(path=ROOT/'data/flow/settings.json'))
    for k,v in paths.items():
        if not config.get(k):config[k]=v
    try:
        c=json.loads(Path(config['weflow_config']).read_text('utf8'))
        api_url='http://127.0.0.1:'+str(int(c.get('httpApiPort',5031)))+'/api/v1/health'
        try:
            request.build_opener(request.ProxyHandler({})).open(api_url,timeout=1).close()
        except request.HTTPError:pass # A reachable API may require a token.
        except OSError:
            exe=config.get('weflow_executable','')
            if exe and Path(exe).is_file() and os.name=='nt':
                env={**os.environ,'WCWD_WEFLOW_EXE':exe}
                subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command','Start-Process -FilePath $env:WCWD_WEFLOW_EXE -WindowStyle Hidden'],env=env,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
    except (OSError,ValueError):pass # The integrated settings page explains missing paths.
    if not health():
        with (DATA/'service.log').open('ab') as log:
            p=subprocess.Popen([sys.executable,'-m','workdesk.assistant','--data',str(DATA)],cwd=ROOT,stdout=log,stderr=log,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for _ in range(40):
            if p.poll() is not None:raise SystemExit('更新助手启动失败，请检查端口8771或日志。')
            if health():break
            time.sleep(.2)
        else:
            p.terminate();raise SystemExit('更新助手启动超时。')
    website=config.get('website','https://desk.plantisland.cn').rstrip('/')
    target=website+'/update'
    if '--local' in sys.argv:target=URL+'/update'
    if '--no-browser' not in sys.argv:webbrowser.open(target)
    print(target)

if __name__=='__main__':main()
