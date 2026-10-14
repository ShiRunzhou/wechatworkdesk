"""Launch only this workspace's local process; never terminate other port owners."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PORT = 8770
URL = f"http://127.0.0.1:{PORT}/"


def health():
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=1) as response:
            result = json.load(response)
            return (result.get("status") == "ok" and result.get("instance") ==
                    hashlib.sha256(str(DATA.resolve()).casefold().encode()).hexdigest())
    except Exception:
        return False


def identity(pid):
    # Fixed script text with integer PID, no command composition from user input.
    script = f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def same_process(saved):
    current = identity(saved["pid"])
    if not current:
        return False
    return (current.get("CreationDate") == saved["identity"].get("CreationDate")
            and current.get("ExecutablePath") == saved["identity"].get("ExecutablePath")
            and current.get("CommandLine") == saved["identity"].get("CommandLine")
            and str(ROOT / ".venv" / "Scripts" / "python.exe").casefold() in current.get("CommandLine", "").casefold()
            and "-m workdesk serve" in current.get("CommandLine", ""))


def service_identity(parent_pid):
    # Windows venv redirectors may create a child Python process owning the listener.
    script = f"@(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique) | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    if not result.stdout.strip():
        return None
    owner = json.loads(result.stdout)
    owners = owner if isinstance(owner, list) else [owner]
    if len(owners) != 1:
        return None
    current = identity(owners[0])
    if (current and (current["ProcessId"] == parent_pid or current.get("ParentProcessId") == parent_pid)
            and str(ROOT / ".venv" / "Scripts" / "python.exe").casefold() in current.get("CommandLine", "").casefold()
            and "-m workdesk serve" in current.get("CommandLine", "")):
        return current
    return None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--page",choices=['/','/update'],default='/')
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    marker = DATA / "runtime.json"
    if args.action == "stop":
        if not marker.exists():
            print("没有本项目登记的后台进程。")
            return
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if same_process(saved):
            current = service_identity(saved["pid"])
            # Old development markers can refer to the venv wrapper; migrate safely to its child.
            stop_pid = current["ProcessId"] if current else saved["pid"]
            script = f"Stop-Process -Id {int(stop_pid)}"
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(20):
                if not health():
                    break
                time.sleep(.1)
            if health():
                raise SystemExit("服务尚未退出，登记保留；请核对后台进程。")
            print("工作事项台已停止，数据保留。")
        else:
            print("登记的进程已退出或身份改变，没有停止其他进程。")
        marker.unlink(missing_ok=True)
        return
    if health():
        if not marker.exists() or not same_process(json.loads(marker.read_text(encoding="utf-8"))):
            raise SystemExit("8770 已有服务运行，无法确认属于本项目。请先核对，不会启动或停止其他服务。")
        saved = json.loads(marker.read_text(encoding="utf-8"))
        current = service_identity(saved["pid"])
        if current:
            marker.write_text(json.dumps({"pid": current["ProcessId"], "identity": current}, ensure_ascii=False), encoding="utf-8")
    else:
        # A failed bind exits; do not kill whoever owns the port.
        with (DATA / "service.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "workdesk", "serve", "--port", str(PORT)],
                                       cwd=ROOT, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(40):
            if process.poll() is not None:
                raise SystemExit("启动失败，请查看 data/service.log；没有停止其他服务。")
            if health():
                current = service_identity(process.pid)
                if not current:
                    raise SystemExit("服务身份未核实，请查看后台进程。")
                marker.write_text(json.dumps({"pid": current["ProcessId"], "identity": current}, ensure_ascii=False), encoding="utf-8")
                break
            time.sleep(.2)
        else:
            process.terminate()
            raise SystemExit("服务未就绪，本次启动进程已退出。")
    if not args.no_browser:
        webbrowser.open(URL.rstrip('/')+args.page)
    print(URL.rstrip('/')+args.page)


if __name__ == "__main__":
    main()
