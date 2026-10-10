from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from workdesk.store import Store, DeskError
from workdesk.server import make_server


class WorkDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "data")
        self.export = self.root / "export"
        self.export.mkdir()
        (self.export / "记录.md").write_text("2026-10-10 09:00 教学秘书：请于10月15日前导出实习生数据。\n", encoding="utf-8")
        (self.export / "附件.txt").write_text("模拟材料，仅用于测试。", encoding="utf-8")
        self.obj = self.store.add_object("模拟教学群")
        self.imp = self.store.register_import(self.obj["id"], self.export)
        self.run = self.store.create_run([self.obj["id"]], "理解教学工作并提炼事项")
        self.context = self.store.context(self.run["id"])
        self.source = self.context["sources"][0]

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self):
        return {
            "coverage": [{"source_id": s["id"], "state": "read", "note": "完整阅读模拟文件"} for s in self.context["sources"]],
            "backgrounds": [{"object_id": self.obj["id"], "summary": "模拟教学工作通知", "role": "实习数据汇总职责待确认", "uncertainties": "需确认本人是否负责",
                             "evidence": [{"source_id": self.source["id"], "locator": "第1行"}]}],
            "matters": [{"object_id": self.obj["id"], "dedup_key": "intern-export-2026", "title": "导出实习生工作数据", "detail": "准备并导出实习生数据。", "notice_at": "2026-10-10T09:00:00+08:00", "deadline": "10月15日前", "owner": "适用人员待确认", "action": "确认本人职责与导出字段", "questions": "是否由本人负责？", "suggested_state": "needs_confirmation",
                         "evidence": [{"source_id": self.source["id"], "locator": "2026-10-10 09:00 教学秘书 第1行"}]}]
        }

    def publish(self):
        self.store.submit(self.run["id"], self.payload())
        return self.store.dashboard()["matters"][0]

    def test_snapshot_not_original_and_same_export_reuses_sources(self):
        old = [(s["id"], s["sha256"]) for s in self.context["sources"]]
        new_imp = self.store.register_import(self.obj["id"], self.export)
        other = self.store.create_run([self.obj["id"]], "更新")
        self.assertEqual(old, [(s["id"], s["sha256"]) for s in self.store.context(other["id"])["sources"]])
        self.assertNotEqual(self.imp["id"], new_imp["id"])
        original = self.export / "记录.md"
        original.write_text("原文件已改变", encoding="utf-8")
        source = next(s for s in self.context["sources"] if s["relative_path"] == "记录.md")
        _, snapshot = self.store.source(source["id"])
        self.assertIn("教学秘书", snapshot.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), source["sha256"])

    def test_same_filename_changed_content_is_new_source(self):
        (self.export / "记录.md").write_text("新通知", encoding="utf-8")
        self.store.register_import(self.obj["id"], self.export)
        run = self.store.create_run([self.obj["id"]], "更新")
        old = next(s["id"] for s in self.context["sources"] if s["relative_path"] == "记录.md")
        new = next(s["id"] for s in self.store.context(run["id"])["sources"] if s["relative_path"] == "记录.md")
        self.assertNotEqual(old, new)

    def test_empty_or_bad_import_is_rejected(self):
        folder = self.root / "empty"
        folder.mkdir()
        with self.assertRaises(DeskError):
            self.store.register_import(self.obj["id"], folder)
        with self.assertRaises(DeskError):
            self.store.register_import(self.obj["id"], self.root)
        with self.assertRaises(DeskError):
            self.store.register_import(self.obj["id"], self.export, "2026-10-11T00:00:00+08:00", "2026-10-01T00:00:00+08:00")

    def test_run_requires_import_for_every_object(self):
        obj = self.store.add_object("没有资料的联系人", "person")
        with self.assertRaises(DeskError):
            self.store.create_run([self.obj["id"], obj["id"]], "阅读")
        self.assertEqual(len(self.store.dashboard()["runs"]), 1)

    def test_coverage_complete_and_atomic(self):
        payload = self.payload()
        payload["coverage"].pop()
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)
        dash = self.store.dashboard()
        self.assertEqual(dash["matters"], [])
        self.assertEqual(dash["runs"][0]["coverage"], [])
        self.assertEqual(dash["runs"][0]["state"], "reading")

    def test_unread_cannot_support_claim_but_partial_can(self):
        payload = self.payload()
        next(c for c in payload["coverage"] if c["source_id"] == self.source["id"])["state"] = "unread"
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)
        next(c for c in payload["coverage"] if c["source_id"] == self.source["id"])["state"] = "partial"
        self.assertEqual(self.store.submit(self.run["id"], payload)["new"], 1)

    def test_wrong_object_or_other_round_evidence_rejected(self):
        other = self.store.add_object("另一群")
        self.store.register_import(other["id"], self.export)
        another = self.store.create_run([other["id"]], "另一群阅读")
        foreign = self.store.context(another["id"])["sources"][0]["id"]
        payload = self.payload()
        payload["matters"][0]["evidence"][0]["source_id"] = foreign
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)
        payload = self.payload()
        payload["matters"][0]["object_id"] = other["id"]
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)

    def test_submit_idempotent_and_no_rewrite(self):
        self.publish()
        self.assertTrue(self.store.submit(self.run["id"], self.payload())["reused"])
        changed = self.payload()
        changed["matters"][0]["title"] = "另一种结果"
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], changed)
        self.assertEqual(len(self.store.dashboard()["matters"]), 1)

    def test_model_cannot_mark_completed(self):
        payload = self.payload()
        payload["matters"][0]["suggested_state"] = "completed"
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)

    def test_malformed_nested_result_rejected_without_partial_write(self):
        payload = self.payload()
        payload["coverage"][0]["source_id"] = []
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)
        payload = self.payload()
        payload["matters"][0]["object_id"] = []
        with self.assertRaises(DeskError):
            self.store.submit(self.run["id"], payload)
        self.assertEqual(self.store.dashboard()["matters"], [])

    def test_incremental_preserves_human_state_and_original_result(self):
        matter = self.publish()
        reviewed = self.store.feedback("matter", matter["id"], {"version": matter["version"], "state": "completed", "note": "已导出至模拟目录"})
        run = self.store.create_run([self.obj["id"]], "通知更新")
        context = self.store.context(run["id"])
        self.assertTrue(all(s["previous_reading"]["state"] == "read" for s in context["sources"]))
        self.assertEqual(context["objects"][0]["existing_matters"][0]["human_note"], "已导出至模拟目录")
        payload = self.payload()
        payload["matters"][0]["detail"] = "增加一个导出字段。"
        self.assertEqual(self.store.submit(run["id"], payload)["changed"], 1)
        latest = self.store.dashboard()["matters"][0]
        self.assertEqual(latest["state"], "completed")
        self.assertEqual(latest["human_note"], reviewed["human_note"])
        self.assertEqual(latest["changed"], 1)
        self.assertEqual(json.loads(self.store.context(self.run["id"])["run"]["result_json"])["matters"][0]["detail"], "准备并导出实习生数据。")

    def test_unchanged_repeat_no_revision_or_loss_of_feedback(self):
        matter = self.publish()
        after = self.store.feedback("matter", matter["id"], {"version": matter["version"], "state": "irrelevant", "note": "此项由其他老师处理"})
        run = self.store.create_run([self.obj["id"]], "复查")
        self.assertEqual(self.store.submit(run["id"], self.payload())["unchanged"], 1)
        current = self.store.dashboard()["matters"][0]
        self.assertEqual(current["version"], after["version"])
        self.assertEqual(current["state"], "irrelevant")

    def test_stale_feedback_and_stale_handoff_blocked(self):
        matter = self.publish()
        self.store.feedback("matter", matter["id"], {"version": 1, "state": "pending"})
        with self.assertRaises(DeskError):
            self.store.feedback("matter", matter["id"], {"version": 1, "state": "completed"})
        with self.assertRaises(DeskError):
            self.store.handoff(matter["id"], 1)

    def test_deferred_requires_future_timezone_time(self):
        matter = self.publish()
        for when in ["", "2020-01-01T00:00:00+08:00", "2099-01-01T00:00:00"]:
            with self.assertRaises(DeskError):
                self.store.feedback("matter", matter["id"], {"version": 1, "state": "deferred", "deferred_until": when})
        self.assertEqual(self.store.feedback("matter", matter["id"], {"version": 1, "state": "deferred", "deferred_until": "2099-01-01T00:00:00+08:00"})["state"], "deferred")

    def test_background_only_confirmed_enters_next_context(self):
        self.publish()
        bg = self.store.dashboard()["backgrounds"][0]
        self.assertEqual(self.store.context(self.run["id"])["objects"][0]["confirmed_backgrounds"], [])
        self.store.feedback("background", bg["id"], {"version": 1, "state": "confirmed", "summary": "由用户纠正的工作背景", "role": "负责汇总", "uncertainties": ""})
        context = self.store.context(self.run["id"])
        self.assertEqual(context["objects"][0]["confirmed_backgrounds"][0]["summary"], "由用户纠正的工作背景")
        self.assertEqual(context["objects"][0]["confirmed_backgrounds"][0]["uncertainties"], "")

    def test_handoff_keeps_status_and_references_and_is_durable(self):
        matter = self.publish()
        handoff = self.store.handoff(matter["id"], matter["version"], "先核对字段，暂不提交")
        self.assertIn("本地快照", handoff["markdown"])
        self.assertIn("先核对字段", handoff["markdown"])
        self.assertEqual(self.store.dashboard()["matters"][0]["state"], "needs_confirmation")
        reopened = Store(self.root / "data")
        self.assertEqual(reopened.dashboard()["handoffs"][0]["id"], handoff["id"])

    def test_empty_matters_is_valid_and_missing_next_round_not_completion(self):
        matter = self.publish()
        run = self.store.create_run([self.obj["id"]], "无新事项")
        payload = self.payload()
        payload["matters"], payload["backgrounds"] = [], []
        self.assertEqual(self.store.submit(run["id"], payload)["new"], 0)
        self.assertEqual(self.store.dashboard()["matters"][0]["state"], matter["state"])

    def test_cli_end_to_end_with_isolated_data(self):
        result_file = self.root / "result.json"
        result_file.write_text(json.dumps(self.payload(), ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, "-m", "workdesk", "--data", str(self.root / "data")]
        proc = subprocess.run(command + ["run", "submit", self.run["id"], "--result", str(result_file)], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(json.loads(proc.stdout)["new"], 1)
        proc = subprocess.run(command + ["status"], capture_output=True)
        self.assertEqual(json.loads(proc.stdout)["matters"][0]["title"], "导出实习生工作数据")


class HttpTests(unittest.TestCase):
    payload = WorkDeskTests.payload
    publish = WorkDeskTests.publish
    def setUp(self):
        WorkDeskTests.setUp(self)
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        WorkDeskTests.tearDown(self)

    def request(self, route, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.url + route, data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def test_http_actual_state_feedback_handoff_and_source_download(self):
        matter = self.publish()
        status, body, _ = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["matters"][0]["title"], matter["title"])
        self.assertEqual(self.request(f"/api/matters/{matter['id']}/feedback", {"version": 1, "state": "pending"})[0], 200)
        status, body, _ = self.request(f"/api/matters/{matter['id']}/handoff", {"version": 2})
        self.assertEqual(status, 200)
        handoff = json.loads(body)
        status, body, headers = self.request(f"/api/handoffs/{handoff['id']}/download")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("推进边界", body.decode())
        status, body, headers = self.request(f"/api/sources/{self.source['id']}/download")
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.source["sha256"])
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_cross_origin_or_wrong_host_no_mutation(self):
        matter = self.publish()
        route = f"/api/matters/{matter['id']}/feedback"
        payload = {"version": 1, "state": "completed"}
        self.assertEqual(self.request(route, payload, {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request(route, payload, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.store.dashboard()["matters"][0]["version"], 1)

    def test_static_and_path_escape(self):
        for path in ["/", "/app.js", "/style.css"]:
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertTrue(body)
        self.assertEqual(self.request("/../store.py")[0], 404)
        self.assertEqual(self.request("/api/sources/../../store.py/download")[0], 404)


if __name__ == "__main__":
    unittest.main()
