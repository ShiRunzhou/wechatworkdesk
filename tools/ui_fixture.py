"""Create clearly labelled synthetic material in test-output; never production data."""
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workdesk.store import Store


def main():
    fixture = ROOT / "test-output" / ("ui-" + uuid.uuid4().hex[:8])
    export = fixture / "export"
    export.mkdir(parents=True)
    (export / "模拟通知.md").write_text("2026-10-10 09:00 教学秘书：请于10月15日前导出实习生数据。\n2026-10-10 10:00 教学秘书：请确认本学期课程安排。\n", encoding="utf-8")
    (export / "未读模拟附件.txt").write_text("此文件仅用于显示未读覆盖。", encoding="utf-8")
    store = Store(fixture / "data")
    for index, name in enumerate(["模拟 · 教学工作群", "模拟 · 工作联系人"]):
        obj = store.add_object(name, "group" if index == 0 else "person")
        store.register_import(obj["id"], export, "2026-10-01T00:00:00+08:00", "2026-10-11T00:00:00+08:00")
        run = store.create_run([obj["id"]], "模拟：了解工作背景并识别待确认事项")
        sources = store.context(run["id"])["sources"]
        source = next(s for s in sources if s["relative_path"] == "模拟通知.md")
        evidence = [{"source_id": source["id"], "locator": "2026-10-10 09:00 教学秘书 · 第1行"}]
        store.submit(run["id"], {
            "coverage": [{"source_id": s["id"], "state": "read" if s["id"] == source["id"] else "unread",
                          "note": "完整阅读两条模拟通知" if s["id"] == source["id"] else "模拟未读材料，用于验证界面明确显示阅读缺口"} for s in sources],
            "backgrounds": [{"object_id": obj["id"], "summary": "用于发布教学安排与实习工作通知；此处为模拟背景。", "role": "是否负责实习数据汇总尚需确认。", "uncertainties": "需确认用户职责。", "evidence": evidence}],
            "matters": [{"object_id": obj["id"], "dedup_key": "intern-data-export", "title": "导出实习生工作数据（模拟）", "detail": "按通知要求整理并导出本学期实习生数据。", "notice_at": "2026-10-10T09:00:00+08:00", "deadline": "10月15日前", "owner": "适用人员待确认", "action": "先确认是否由你负责，再核对导出字段。", "questions": "尚无是否已完成的证据。", "suggested_state": "needs_confirmation", "evidence": evidence}]
        })
    (ROOT / "test-output" / "ui-fixture.json").write_text(json.dumps({"data": str(store.root)}, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"data": str(store.root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
