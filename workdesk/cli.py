from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .store import Store, DeskError


def output(value, path=None):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"saved_to": str(Path(path).resolve())}, ensure_ascii=False))
    else:
        print(text)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="工作事项台：登记导出、建立阅读任务、提交结果")
    parser.add_argument("--data", default=str(Path(__file__).resolve().parents[1] / "data"))
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    obj = commands.add_parser("object", help="登记沟通对象").add_subparsers(dest="action", required=True)
    add = obj.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--kind", choices=["group", "person"], default="group")
    add.add_argument("--external-id", default="")
    imp = commands.add_parser("import", help="登记并快照导出文件").add_subparsers(dest="action", required=True)
    register = imp.add_parser("register")
    register.add_argument("--object-id", required=True)
    register.add_argument("--path", required=True)
    register.add_argument("--start", default="")
    register.add_argument("--end", default="")
    runs = commands.add_parser("run", help="Agent 阅读与提交结果").add_subparsers(dest="action", required=True)
    create = runs.add_parser("create")
    create.add_argument("--object-id", action="append", required=True)
    create.add_argument("--goal", required=True)
    context = runs.add_parser("context")
    context.add_argument("run_id")
    context.add_argument("--out")
    submit = runs.add_parser("submit")
    submit.add_argument("run_id")
    submit.add_argument("--result", required=True)
    sources = commands.add_parser("source", help="定位原始资料快照").add_subparsers(dest="action", required=True)
    locate = sources.add_parser("locate")
    locate.add_argument("source_id")
    commands.add_parser("status")
    memory=commands.add_parser('memory',help='工作记忆与历史档案').add_subparsers(dest='action',required=True)
    memory.add_parser('state');memory.add_parser('export');memory.add_parser('scan')
    search=memory.add_parser('search');search.add_argument('query');search.add_argument('--object-id',default='')
    storage=commands.add_parser('storage',help='资料清理').add_subparsers(dest='action',required=True)
    storage.add_parser('preview')
    policy=storage.add_parser('policy');policy.add_argument('--days',type=int,default=90);policy.add_argument('--auto',action='store_true')
    clean=storage.add_parser('clean');clean.add_argument('--selection',required=True,help='包含token、ids、confirmed的预览选择JSON文件')
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--website-config", help="启用私有网站模式，指定独立登录配置")
    args = parser.parse_args()
    try:
        store = Store(args.data)
        if args.command == "object":
            output(store.add_object(args.name, args.kind, args.external_id))
        elif args.command == "import":
            output(store.register_import(args.object_id, args.path, args.start, args.end))
        elif args.command == "run" and args.action == "create":
            output(store.create_run(args.object_id, args.goal))
        elif args.command == "run" and args.action == "context":
            output(store.context(args.run_id), args.out)
        elif args.command == "run" and args.action == "submit":
            payload = json.loads(Path(args.result).read_text(encoding="utf-8-sig"))
            output(store.submit(args.run_id, payload))
        elif args.command == "source":
            source, file = store.source(args.source_id)
            output({**source, "snapshot_path": str(file), "note": "请用相应工具读取该快照；此命令不表示已经阅读。"})
        elif args.command == "status":
            output(store.dashboard())
        elif args.command=='memory':
            if args.action=='state':output(store.memory_state())
            elif args.action=='export':output(store.export_memory())
            elif args.action=='scan':output(store.scan_feedback())
            else:output({'history':store.search_memory(args.query,args.object_id)})
        elif args.command=='storage':
            if args.action=='preview':output(store.storage_preview())
            elif args.action=='policy':output(store.save_storage_policy({'retention_days':args.days,'auto_enabled':args.auto}))
            else:output(store.storage_clean(json.loads(Path(args.selection).read_text('utf-8-sig'))))
        else:
            from .server import serve as start_server
            start_server(store, args.port, args.website_config)
    except (DeskError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
