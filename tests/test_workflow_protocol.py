"""Public temporary repositories exercise protocol and knowledge contracts."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check
import workflow
import workflow_protocol as protocol
import workflow_knowledge as knowledge


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(
            ["git", "init", "-q", str(self.root)], check=True, capture_output=True
        )
        for name in check.REQUIRED:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        (self.root / ".gitignore").write_text(
            ".env\n.env.*\n!.env.example\n.local/\n.workflow/\n*.sqlite3\n",
            encoding="utf-8",
        )
        (self.root / "src").mkdir()
        (self.root / "src/example.py").write_text("value = 1\n", encoding="utf-8")
        self.record = workflow.intake(self.root, "sample", "明确请求", "范围", ["结果"])

    def align(self):
        return protocol.align(
            self.root, "sample", "用户原始请求", ["修改 example"], ["保留其他行为"]
        )

    def planned(self):
        self.align()
        protocol.exempt(self.root, "sample", "仅维护开发基础设施，无业务行为变化")
        return protocol.plan(
            self.root,
            "sample",
            "复用 example",
            "最小调整",
            "权限不变",
            ["实现并验证一个结果"],
            "运行 example 测试",
            "恢复本次文件变化",
        )

    def story(self, asset_id="sample-story"):
        return dict(
            id=asset_id,
            title="示例业务故事线",
            source="根据当前公开源码观察",
            tags=["example", "业务"],
            status="observed",
            sources=["src/example.py"],
            actors=["用户"],
            preconditions=["有效会话"],
            invariants=["不扩大权限"],
            scenarios=[
                dict(
                    id="success",
                    title="正常完成",
                    given="任务存在",
                    when="用户执行",
                    then="得到结果",
                    **{"from": "ready", "to": "done"},
                    tests=["test_example.ExampleTests.test_behavior"],
                )
            ],
        )

    def experience(self, asset_id="sample-lesson", supersedes=None):
        return dict(
            id=asset_id,
            title="示例排障经验",
            source="可复现测试",
            tags=["example", "排障"],
            status="active",
            sources=["src/example.py"],
            evidence="临时环境中的实际观察",
            outcome="observed",
            recommendation="先确认输入",
            applies_when=["example 输入变化"],
            counterexamples=["不适用于外部服务错误"],
            supersedes=supersedes or [],
        )

    def put(self, kind, payload):
        path = self.root / "asset-input.json"
        workflow.save_json(path, payload)
        return knowledge.put(self.root, kind, path.name)


class ProtocolTests(RepositoryCase):
    def test_invalid_protocol_cannot_silently_pass_validation(self):
        self.record["protocol"] = {"version": 99, "authorization": "broken"}
        with self.assertRaises(ValueError):
            workflow.validate_record(self.record)

    def test_authorization_change_invalidates_intent_evidence(self):
        self.record["protocol"] = {"authorization": {"allowed": ["old"]}}
        old = workflow.intent_fingerprint(self.record)
        self.record["protocol"]["authorization"]["allowed"] = ["new"]
        self.assertNotEqual(old, workflow.intent_fingerprint(self.record))

    def test_question_and_status_preserve_current_task_and_evidence(self):
        self.planned()
        path = workflow.record_path(self.root, "sample")
        before = path.read_bytes()
        for kind in ("question", "status"):
            result = protocol.request(self.root, kind, "进展如何", "当前用户消息")
            self.assertEqual(result["action"], "answer_then_continue")
            self.assertEqual(result["task"]["key"], "sample")
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            protocol.readiness(self.root, workflow.load_record(self.root, "sample")), []
        )

    def test_new_and_analysis_route_without_creating_or_selecting_task(self):
        before = sorted((self.root / "docs/changes").iterdir())
        for kind, action in (("new", "intake"), ("analysis", "read_only")):
            self.assertEqual(
                protocol.request(self.root, kind, "新问题", "用户")["action"], action
            )
        self.assertEqual(sorted((self.root / "docs/changes").iterdir()), before)

    def test_pause_continue_cancel_and_retry_have_explicit_sources(self):
        self.planned()
        paused = protocol.request(
            self.root, "pause", "暂停", "用户明确暂停", "sample", "pause-1"
        )
        self.assertEqual(paused["protocol"]["state"], "paused")
        self.assertEqual(
            protocol.request(self.root, "question", "进度如何", "用户", "sample")[
                "action"
            ],
            "answer_only",
        )
        self.assertTrue(protocol.readiness(self.root, paused))
        self.assertEqual(
            protocol.summary(self.root, "sample")["protocol"]["state"], "paused"
        )
        with self.assertRaises(ValueError):
            workflow.progress(self.root, "sample", "隐式继续")
        with self.assertRaises(ValueError):
            workflow.resume(self.root)
        continued = protocol.request(
            self.root, "continue", "继续", "用户继续", "sample", "continue-1"
        )
        retried = protocol.request(
            self.root, "pause", "暂停", "用户明确暂停", "sample", "pause-1"
        )
        self.assertEqual(retried, continued)
        with self.assertRaises(ValueError):
            protocol.request(
                self.root, "cancel", "取消", "用户取消", "sample", "pause-1"
            )
        cancelled = protocol.request(self.root, "cancel", "取消", "用户取消", "sample")
        self.assertEqual(cancelled["protocol"]["state"], "cancelled")
        with self.assertRaises(ValueError):
            protocol.request(self.root, "continue", "继续", "用户继续", "sample")
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=证据"])

    def test_correction_requires_rework_then_alignment_and_new_plan(self):
        original = self.planned()
        changed = protocol.request(
            self.root, "correction", "用户收窄范围", "用户消息", "sample", "change-1"
        )
        self.assertEqual(changed["protocol"]["authorization"]["status"], "needs_review")
        self.assertIsNone(changed["protocol"]["plan"])
        self.assertTrue(protocol.readiness(self.root, changed))
        pending = protocol.summary(self.root, "sample")["protocol"]["pending"][0]
        self.assertEqual(pending["message"], "用户收窄范围")
        self.assertEqual(pending["source"], "用户消息")
        with self.assertRaises(ValueError):
            self.align()
        changed = workflow.rework(
            self.root, "sample", "新请求", "新范围", "用户纠正", ["AC1=新结果"]
        )
        prior_protocol = changed["history"][-1]["previous"]["protocol"]
        self.assertEqual(prior_protocol["authorization"]["status"], "needs_review")
        self.assertEqual(
            prior_protocol["events"][-1]["previous"]["protocol"]["authorization"],
            original["protocol"]["authorization"],
        )
        self.assertEqual(
            prior_protocol["events"][-1]["previous"]["protocol"]["plan"],
            original["protocol"]["plan"],
        )
        aligned = self.align()
        self.assertEqual(aligned["protocol"]["pending"], [])
        self.assertTrue(protocol.readiness(self.root, aligned))
        self.assertEqual(aligned["baseline"], original["baseline"])

    def test_alignment_requires_source_and_preserved_scope_without_writing_failure(
        self,
    ):
        before = workflow.record_path(self.root, "sample").read_bytes()
        for source, allowed, preserved in (
            ("", ["change"], ["keep"]),
            ("user", [], ["keep"]),
            ("user", ["change"], []),
        ):
            with self.assertRaises(ValueError):
                protocol.align(self.root, "sample", source, allowed, preserved)
        self.assertEqual(workflow.record_path(self.root, "sample").read_bytes(), before)

    def test_plan_versions_and_prior_decisions_are_preserved(self):
        first = self.planned()
        second = protocol.plan(
            self.root,
            "sample",
            "更多复用",
            "更小调整",
            "保留",
            ["切片"],
            "测试",
            "恢复",
        )
        self.assertEqual(
            second["protocol"]["events"][-1]["previous"], first["protocol"]["plan"]
        )
        self.assertNotEqual(
            workflow.intent_fingerprint(first), workflow.intent_fingerprint(second)
        )
        self.assertEqual(protocol.readiness(self.root, second), [])
        aligned = self.align()
        self.assertIsNone(aligned["protocol"]["plan"])

    def test_multiple_active_tasks_never_guess_target(self):
        self.planned()
        workflow.intake(self.root, "another", "请求", "范围", ["条件"])
        with self.assertRaises(ValueError):
            protocol.request(self.root, "status", "进度", "用户")
        self.assertEqual(
            protocol.request(self.root, "status", "进度", "用户", "sample")["task"][
                "key"
            ],
            "sample",
        )

    def test_story_link_detects_source_drift_and_changed_revision(self):
        self.planned()
        self.put("story", self.story())
        linked = protocol.link(self.root, "sample", "AC1", "sample-story", "success")
        self.assertEqual(protocol.readiness(self.root, linked), [])
        with self.assertRaises(ValueError):
            protocol.link(self.root, "sample", "AC2", "sample-story", "success")
        with self.assertRaises(ValueError):
            protocol.link(self.root, "sample", "AC1", "sample-story", "missing")
        (self.root / "src/example.py").write_text("value = 2\n", encoding="utf-8")
        self.assertIn("已过期", " ".join(protocol.readiness(self.root, linked)))
        with self.assertRaises(ValueError):
            protocol.link(self.root, "sample", "AC1", "sample-story", "success")
        self.put("story", self.story())
        self.assertTrue(protocol.readiness(self.root, linked))
        relinked = protocol.link(self.root, "sample", "AC1", "sample-story", "success")
        self.assertEqual(protocol.readiness(self.root, relinked), [])
        self.assertEqual(relinked["protocol"]["stories"][0]["revision"], 2)

    def test_draft_story_cannot_become_delivery_contract(self):
        self.planned()
        self.put("story", {**self.story(), "status": "draft"})
        with self.assertRaises(ValueError):
            protocol.link(self.root, "sample", "AC1", "sample-story", "success")

    def test_summary_keeps_current_boundaries_but_omits_event_and_rework_bodies(self):
        self.planned()
        workflow.rework(self.root, "sample", "最新", "最新范围", "用户", ["AC1=结果"])
        self.planned()
        result = protocol.summary(self.root, "sample")
        self.assertEqual(result["request"], "最新")
        self.assertEqual(result["history"]["revisions"], 1)
        self.assertNotIn("events", result["protocol"])
        self.assertNotIn("previous", json.dumps(result))
        self.assertFalse(result["verification"]["fresh"])

    def test_protocol_preflight_blocks_check_before_running_tools(self):
        self.align()
        with (
            patch.object(check, "ROOT", self.root),
            patch.object(sys, "argv", ["check.py", "--record", "sample"]),
            patch.object(check, "run_step") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(check.main(), 1)
        run.assert_not_called()

    def test_changed_criteria_drop_story_links_and_rework_clears_exemption(self):
        self.planned()
        self.put("story", self.story())
        protocol.link(self.root, "sample", "AC1", "sample-story", "success")
        result = workflow.rework(
            self.root, "sample", "新请求", "范围", "用户", ["AC1=不同结果"]
        )
        self.assertEqual(result["protocol"]["stories"], [])
        self.assertTrue(result["history"][-1]["previous"]["protocol"]["stories"])
        self.planned()
        result = workflow.rework(
            self.root, "sample", "新请求", "范围", "用户", ["AC1=不同结果"]
        )
        self.assertEqual(result["protocol"]["story_exemption"], "")

    def test_v1_alignment_is_explicit_and_preserves_original_snapshot(self):
        original = self.record
        original["version"] = 1
        for field in ("revision", "history", "remaining_milestones", "baseline"):
            original.pop(field)
        original["acceptance"] = [dict(id="AC1", criterion="旧条件", evidence="")]
        workflow.save_json(workflow.record_path(self.root, "sample"), original)
        self.assertEqual(workflow.resume(self.root, "sample"), original)
        aligned = self.align()
        self.assertEqual(aligned["version"], 2)
        self.assertEqual(aligned["history"][0]["previous"], original)
        self.assertEqual(aligned["protocol"]["authorization"]["source"], "用户原始请求")

    def test_contract_change_during_check_cannot_overwrite_new_request_state(self):
        self.planned()
        workflow.map_ac(
            self.root,
            "sample",
            "AC1",
            "用户",
            tests=["test_example.ExampleTests.test_behavior"],
        )

        def execute(root, name, command):
            if name == "lint":
                protocol.request(
                    root, "correction", "执行中出现纠正", "用户消息", "sample"
                )
            if name == "tests":
                workflow.save_json(
                    root / command[command.index("--report") + 1],
                    {
                        "tests_run": 1,
                        "cases": [
                            {
                                "id": "test_example.ExampleTests.test_behavior",
                                "status": "passed",
                            }
                        ],
                    },
                )
            return {"exit_code": 0}

        with (
            patch.object(check, "ROOT", self.root),
            patch.object(sys, "argv", ["check.py", "--record", "sample"]),
            patch.object(check, "run_step", side_effect=execute),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as error:
                check.main()
        self.assertEqual(error.exception.code, 1)
        current = workflow.load_record(self.root, "sample")
        self.assertEqual(current["protocol"]["authorization"]["status"], "needs_review")
        self.assertEqual(current["verification"]["status"], "pending")


class KnowledgeTests(RepositoryCase):
    def test_asset_update_keeps_history_and_changes_digest(self):
        first = self.put("story", self.story())
        second = self.put(
            "story",
            {
                **self.story(),
                "title": "新目标",
                "source": "用户确认新目标",
                "status": "confirmed",
            },
        )
        self.assertNotEqual(first["digest"], second["digest"])
        asset = knowledge.load_asset(self.root, "story", "sample-story")
        self.assertEqual(asset["history"][0]["previous"]["status"], "observed")
        self.assertEqual(asset["revision"], 2)
        with self.assertRaises(ValueError):
            knowledge.put(self.root, "story", "docs/storylines/sample-story.json")

    def test_bad_assets_and_duplicate_scenarios_do_not_write(self):
        bad = self.story()
        bad["scenarios"] *= 2
        for payload in (
            bad,
            {**self.story(), "invariants": []},
            {**self.story(), "id": "../outside"},
        ):
            with self.assertRaises(ValueError):
                self.put("story", payload)
        self.assertFalse((self.root / "docs/storylines/sample-story.json").exists())

    def test_private_and_outside_sources_rejected_before_read(self):
        names = [
            ".env",
            ".local/auth.sqlite3",
            "../outside",
            "C:/secret.txt",
            ".git/config",
            "nested/.ENV",
            "src\\example.py",
            "src/example.py:stream",
            ".workflow/check.json",
            ".local./secret.txt",
            "NUL",
            "src/CON.txt",
        ]
        for name in names:
            with self.subTest(name=name), patch.object(Path, "read_text") as read:
                with self.assertRaises(ValueError):
                    knowledge.read_public(self.root, name)
                read.assert_not_called()
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                knowledge.read_public(self.root, "src/example.py")

    def test_experience_supersession_cycle_and_missing_target_rejected(self):
        self.put("experience", self.experience("first"))
        self.put("experience", self.experience("second", ["first"]))
        with self.assertRaises(ValueError):
            self.put("experience", self.experience("first", ["second"]))
        with self.assertRaises(ValueError):
            self.put("experience", self.experience("third", ["missing"]))
        self.assertEqual(
            knowledge.load_asset(self.root, "experience", "first")["supersedes"], []
        )

    def test_resolved_alias_into_private_directory_is_never_read(self):
        resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            if path == self.root / "src/example.py":
                return self.root / ".local/private.txt"
            return resolve(path, *args, **kwargs)

        with (
            patch.object(Path, "resolve", autospec=True, side_effect=redirected),
            patch.object(Path, "read_text") as read,
        ):
            with self.assertRaises(ValueError):
                knowledge.read_public(self.root, "src/example.py")
            read.assert_not_called()

    def test_experience_retrieval_filters_stale_rejected_and_superseded(self):
        self.put("experience", self.experience("old"))
        self.put("experience", self.experience("new", ["old"]))
        self.put("experience", {**self.experience("rejected"), "outcome": "rejected"})
        result = knowledge.context(self.root, query="example", level="related")
        self.assertEqual([item["id"] for item in result["entries"]], ["new"])
        self.assertEqual(
            {item["id"] for item in result["omitted"]}, {"old", "rejected"}
        )
        (self.root / "src/example.py").write_text("changed", encoding="utf-8")
        result = knowledge.context(self.root, query="example")
        self.assertEqual(result["entries"], [])
        self.assertEqual(
            len(
                knowledge.context(self.root, query="example", include_inactive=True)[
                    "entries"
                ]
            ),
            3,
        )

    def test_lessons_require_evidence_applicability_and_counterexamples(self):
        for field, value in (
            ("evidence", ""),
            ("applies_when", []),
            ("counterexamples", []),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.put("experience", {**self.experience(), field: value})

    def test_progressive_context_and_budget_never_truncate_contracts(self):
        self.planned()
        self.put("story", self.story())
        protocol.link(self.root, "sample", "AC1", "sample-story", "success")
        workflow.save_json(
            self.root / "docs/context-map.json",
            dict(
                version=1,
                modules=[
                    dict(
                        id="example",
                        title="示例",
                        tags=["example"],
                        paths=["src/example.py"],
                    )
                ],
            ),
        )
        overview = knowledge.context(self.root, "sample")
        self.assertTrue(all("content" not in item for item in overview["entries"]))
        related = knowledge.context(
            self.root, "sample", query="example", level="related"
        )
        self.assertIn(
            "content",
            next(item for item in related["entries"] if item["kind"] == "story"),
        )
        detail = knowledge.context(self.root, "sample", query="example", level="detail")
        self.assertIn(
            "value = 1",
            next(item for item in detail["entries"] if item["kind"] == "module")[
                "files"
            ][0]["content"],
        )
        bounded = knowledge.context(
            self.root,
            "sample",
            query="unrelated",
            level="related",
            max_chars=200,
            limit=1,
        )
        self.assertEqual(bounded["entries"][0]["id"], "sample-story")
        self.assertIn("deferred", bounded["entries"][0])
        self.assertEqual(
            bounded["task"]["protocol"]["authorization"]["preserved"], ["保留其他行为"]
        )
        self.assertLessEqual(bounded["budget"]["used_chars"], 200)

    def test_retrieval_ignores_forged_index_and_reports_linked_stale_story(self):
        self.planned()
        self.put("story", self.story())
        protocol.link(self.root, "sample", "AC1", "sample-story", "success")
        knowledge.index(self.root)
        workflow.save_json(
            self.root / ".workflow/context-index.json",
            {"entries": [{"content": "FORGED"}]},
        )
        (self.root / "src/example.py").write_text("changed", encoding="utf-8")
        result = knowledge.context(self.root, "sample", query="unrelated")
        self.assertTrue(result["entries"][0]["gaps"])
        self.assertTrue(result["task"]["readiness_gaps"])
        self.assertNotIn("FORGED", json.dumps(result))

    def test_context_map_and_doctor_reject_bad_references(self):
        for name in (".env", "../outside", "missing.py"):
            workflow.save_json(
                self.root / "docs/context-map.json",
                dict(
                    version=1,
                    modules=[
                        dict(id="example", title="模块", tags=["example"], paths=[name])
                    ],
                ),
            )
            self.assertTrue(knowledge.doctor(self.root))

    def test_chinese_search_and_detail_requires_specific_context(self):
        self.put("experience", self.experience())
        self.assertEqual(
            len(knowledge.context(self.root, query="如何排障")["entries"]), 1
        )
        with self.assertRaises(ValueError):
            knowledge.context(self.root, level="detail")


class CliTests(RepositoryCase):
    def cli(self, script, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(self.root / "scripts" / script), *args],
            cwd=self.root,
            capture_output=True,
            encoding="utf-8",
            timeout=45,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def test_real_cli_protocol_story_context_check_and_completion(self):
        for name in (
            "workflow.py",
            "check.py",
            "run_tests.py",
            "workflow_protocol.py",
            "workflow_knowledge.py",
        ):
            (self.root / "scripts" / name).write_bytes(
                (workflow.ROOT / "scripts" / name).read_bytes()
            )
        (self.root / "pyproject.toml").write_bytes(
            (workflow.ROOT / "pyproject.toml").read_bytes()
        )
        (self.root / "tests").mkdir()
        (self.root / "tests/test_example.py").write_text(
            "import unittest\nfrom example import value\nclass ExampleTests(unittest.TestCase):\n    def test_behavior(self):\n        self.assertEqual(value, 1)\n",
            encoding="utf-8",
        )
        self.cli(
            "workflow.py",
            "align",
            "sample",
            "--source",
            "user",
            "--allow",
            "example",
            "--preserve",
            "permissions",
        )
        self.cli(
            "workflow.py",
            "plan",
            "sample",
            "--reuse",
            "existing",
            "--changes",
            "one behavior",
            "--preserve",
            "other behavior",
            "--step",
            "verify",
            "--verification",
            "test",
            "--rollback",
            "restore",
        )
        workflow.save_json(self.root / "asset-input.json", self.story())
        self.cli("workflow.py", "asset", "put", "story", "--file", "asset-input.json")
        self.cli(
            "workflow.py",
            "link",
            "sample",
            "--ac",
            "AC1",
            "--story",
            "sample-story",
            "--scenario",
            "success",
        )
        self.cli(
            "workflow.py",
            "map",
            "sample",
            "--ac",
            "AC1",
            "--source",
            "user",
            "--test",
            "test_example.ExampleTests.test_behavior",
        )
        self.cli("workflow.py", "context", "sample", "--level", "related")
        self.cli("workflow.py", "index")
        self.cli("check.py", "--record", "sample")
        result = json.loads(
            self.cli(
                "workflow.py",
                "completion",
                "sample",
                "--result",
                "done",
                "--evidence",
                "AC1=real CLI test passed",
            )
        )
        self.assertEqual(result["acceptance_status"], "pending")
        current = json.loads(self.cli("workflow.py", "resume", "sample"))
        self.assertTrue(current["verification"]["fresh"])
        self.assertIsInstance(current["history"], dict)
        full = json.loads(self.cli("workflow.py", "resume", "sample", "--full"))
        self.assertIsInstance(full["history"], list)
        self.cli(
            "workflow.py",
            "request",
            "sample",
            "--kind",
            "status",
            "--message",
            "status",
            "--source",
            "user",
        )
        self.assertEqual(
            json.loads(self.cli("workflow.py", "resume", "sample", "--full")), full
        )


if __name__ == "__main__":
    unittest.main()
