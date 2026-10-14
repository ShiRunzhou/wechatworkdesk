"""Start the device assistant without replacing another port owner's process."""
import json
import subprocess
import sys
import time
import webbrowser
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
    if '--no-browser' not in sys.argv:webbrowser.open(URL)
    print(URL)

if __name__=='__main__':main()
