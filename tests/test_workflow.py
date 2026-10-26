"""Workflow contract tests use temporary repositories, never personal data."""

from pathlib import Path
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check import sensitive_paths
import check
import run_tests
import workflow


class HygieneTests(unittest.TestCase):
    def test_private_runtime_paths_are_blocked_but_template_is_allowed(self):
        paths = [
            ".env",
            ".env.production",
            ".local/auth.sqlite3",
            "backup/knowledge.sqlite3",
            ".workflow/check.json",
            ".env.example",
            "src/evan_agent/auth.py",
        ]
        self.assertEqual(sensitive_paths(paths), paths[:5])

    def test_nested_and_case_variants_and_database_sidecars(self):
        paths = [
            "nested/.ENV",
            "copy/.LOCAL/AUTH.SQLITE3",
            "tmp/cache.db-wal",
            "tmp/cache.sqlite3-journal",
            "nested/.env.example",
            "test_database.py",
        ]
        self.assertEqual(sensitive_paths(paths), paths[:4])


class WorkflowTests(unittest.TestCase):
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

    def start(self, key="sample", mode="medium"):
        return workflow.intake(
            self.root, key, "用户请求", "只改授权行为", ["可观察结果"], mode
        )

    def verified(self, scope="full"):
        workflow.map_ac(
            self.root,
            "sample",
            "AC1",
            "测试需求",
            tests=["test_example.ExampleTests.test_behavior"],
        )
        record = workflow.load_record(self.root, "sample")
        record["verification"] = {
            "status": "passed",
            "scope": scope,
            "fingerprint": check.fingerprint(self.root),
            "intent_fingerprint": workflow.intent_fingerprint(record),
            "tests": {
                "tests_run": 1,
                "cases": [
                    {
                        "id": "test_example.ExampleTests.test_behavior",
                        "status": "passed",
                    }
                ],
            },
        }
        workflow.evaluate_acceptance(record, record["verification"])
        workflow.save_json(workflow.record_path(self.root, "sample"), record)

    def run_check(self, args, returncode=0, cases=None, write_report=True):
        def execute(root, name, command):
            if name == "tests" and write_report:
                outcomes = (
                    cases
                    if cases is not None
                    else [
                        {
                            "id": "test_example.ExampleTests.test_behavior",
                            "status": "failed" if returncode else "passed",
                        }
                    ]
                )
                workflow.save_json(
                    root / command[command.index("--report") + 1],
                    {"tests_run": len(outcomes), "cases": outcomes},
                )
            return {"exit_code": returncode}

        with (
            patch.object(check, "ROOT", self.root),
            patch.object(sys, "argv", ["check.py", *args]),
            patch.object(check, "run_step", side_effect=execute) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return check.main(), run.call_count

    def test_doctor_blocks_tracked_secrets_without_printing_contents(self):
        self.assertEqual(check.doctor(self.root), [])
        (self.root / ".env").write_text("DO_NOT_PRINT_THIS_CONTENT", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", ".env"], cwd=self.root, check=True, capture_output=True
        )
        errors = "\n".join(check.doctor(self.root))
        self.assertIn(".env", errors)
        self.assertNotIn("DO_NOT_PRINT", errors)

    def test_doctor_finds_broken_context_links_and_invalid_records(self):
        (self.root / "AGENTS.md").write_text(
            "[missing](docs/no-such-guide.md)", encoding="utf-8"
        )
        workflow.save_json(self.root / "docs/changes/bad.json", {"version": 99})
        errors = "\n".join(check.doctor(self.root))
        self.assertIn("上下文链接失效", errors)
        self.assertIn("无效任务记录", errors)

    def test_intake_preserves_original_and_rejects_duplicate_or_traversal(self):
        original = self.start()
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(workflow.load_record(self.root, "sample"), original)
        for key in ("../outside", "a/b", "", "UPPER", "a" * 81):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.start(key)

    def test_blank_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            workflow.intake(self.root, "sample", "", "scope", ["criterion"])
        with self.assertRaises(ValueError):
            workflow.intake(self.root, "sample", "request", "scope", [" "])

    def test_doctor_rejects_non_object_record_and_bad_verification(self):
        workflow.save_json(self.root / "docs/changes/bad.json", [])
        self.assertTrue(check.doctor(self.root))
        record = self.start()
        record["verification"] = []
        with self.assertRaises(ValueError):
            workflow.validate_record(record)

    def test_resume_refuses_to_guess_multiple_active_tasks(self):
        self.start()
        self.assertEqual(workflow.resume(self.root)["key"], "sample")
        self.start("second")
        with self.assertRaises(ValueError):
            workflow.resume(self.root)
        self.assertEqual(workflow.resume(self.root, "second")["key"], "second")

    def test_progress_keeps_context_and_invalidates_verification(self):
        self.start()
        self.verified()
        workflow.progress(
            self.root, "sample", "下一步", decision="保留行为", gap="未运行 Linux"
        )
        result = workflow.progress(self.root, "sample", "再下一步")
        self.assertEqual(result["request"], "用户请求")
        self.assertEqual(result["decisions"], ["保留行为"])
        self.assertEqual(result["gaps"], ["未运行 Linux"])
        self.assertEqual(result["verification"]["status"], "pending")

    def test_completion_rejects_missing_or_stale_verification(self):
        self.start()
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=证据"])
        self.verified()
        (self.root / "src/example.py").write_text("value = 2\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=证据"])

    def test_contract_change_invalidates_verification(self):
        self.start()
        self.verified()
        record = workflow.load_record(self.root, "sample")
        record["acceptance"][0]["criterion"] = "另一项行为"
        workflow.save_json(workflow.record_path(self.root, "sample"), record)
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=旧证据"])

    def test_completion_rechecks_index_after_successful_verification(self):
        self.start()
        self.verified()
        (self.root / ".env").write_text("fake-only", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", ".env"], cwd=self.root, check=True, capture_output=True
        )
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=旧证据"])

    def test_completion_requires_all_criteria_and_never_accepts_for_user(self):
        self.start()
        self.verified()
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC2=错误编号"])
        result = workflow.complete(self.root, "sample", "完成", ["AC1=实际结果"])
        self.assertEqual(result["implementation"], "completed")
        self.assertEqual(result["acceptance_status"], "pending")
        self.assertEqual(workflow.resume(self.root, "sample"), result)

    def test_full_completion_rejects_targeted_verification(self):
        self.start(mode="full")
        self.verified(scope="targeted")
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=仅局部"])

    def test_check_records_failure_and_reopens_completed_task(self):
        self.start()
        self.verified()
        workflow.complete(self.root, "sample", "完成", ["AC1=实际结果"])
        code, calls = self.run_check(["--record", "sample"], returncode=1)
        self.assertEqual((code, calls), (1, 2))
        record = workflow.load_record(self.root, "sample")
        self.assertEqual(record["verification"]["status"], "failed")
        self.assertEqual(record["implementation"], "in_progress")
        report = json.loads(
            (self.root / ".workflow/check.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["checks"][0]["exit_code"], 1)

    def test_docs_only_is_explicit_low_scope_and_can_complete(self):
        self.start(mode="low")
        workflow.map_ac(
            self.root, "sample", "AC1", "文档要求", manual="核对文档内容和引用"
        )
        workflow.observe(self.root, "sample", "AC1", "已核对内容与链接")
        code, calls = self.run_check(["--record", "sample", "--docs-only"])
        self.assertEqual((code, calls), (0, 0))
        result = workflow.complete(self.root, "sample", "文档完成", ["AC1=doctor"])
        self.assertEqual(result["verification"]["scope"], "doctor")

    def test_docs_only_rejects_changed_code(self):
        self.start(mode="low")
        (self.root / "src/example.py").write_text("value = 2\n", encoding="utf-8")
        code, calls = self.run_check(["--record", "sample", "--docs-only"])
        self.assertEqual((code, calls), (1, 0))

    def test_docs_only_rejects_untracked_code(self):
        self.start(mode="low")
        (self.root / "src/new.py").write_text("value = 3\n", encoding="utf-8")
        code, calls = self.run_check(["--record", "sample", "--docs-only"])
        self.assertEqual((code, calls), (1, 0))

    def test_docs_only_rejects_deleted_code(self):
        self.start(mode="low")
        (self.root / "src/example.py").unlink()
        code, calls = self.run_check(["--record", "sample", "--docs-only"])
        self.assertEqual((code, calls), (1, 0))

    def test_full_check_cannot_be_downgraded(self):
        self.start(mode="full")
        for extra in (["--docs-only"], ["--test", "test_auth"]):
            with (
                self.subTest(extra=extra),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                self.run_check(["--record", "sample", *extra])
            self.assertEqual(error.exception.code, 1)

    def test_private_runtime_files_do_not_enter_fingerprint(self):
        before = check.fingerprint(self.root)
        (self.root / ".env").write_text("not-a-real-secret", encoding="utf-8")
        self.assertEqual(check.fingerprint(self.root), before)

    def test_rework_archives_previous_contract_and_invalidates_acceptance(self):
        self.start()
        workflow.progress(
            self.root,
            "sample",
            "下一步",
            decision="旧约定",
            gap="旧缺口",
            milestones=["旧里程碑"],
        )
        self.verified()
        old = workflow.complete(self.root, "sample", "旧实现", ["AC1=旧证据"])
        old.update(acceptance_status="accepted", user_confirmation="用户确认旧版")
        workflow.save_json(workflow.record_path(self.root, "sample"), old)
        result = workflow.rework(
            self.root,
            "sample",
            "修改后的请求",
            "新的允许范围",
            "用户本轮纠正",
            ["AC1=新的可观察结果", "AC2=新增条件"],
            milestones=["实现新行为", "回归"],
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(
            result["history"][-1]["previous"],
            {name: value for name, value in old.items() if name != "history"},
        )
        self.assertEqual(result["history"][-1]["source"], "用户本轮纠正")
        self.assertEqual(
            result["history"][-1]["acceptance_changes"],
            {"AC1": "replaced", "AC2": "added"},
        )
        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["acceptance"][0]["tests"], [])
        self.assertEqual(result["acceptance"][0]["evidence"], "")
        self.assertEqual(result["verification"]["status"], "pending")
        self.assertEqual(result["acceptance_status"], "pending")
        self.assertNotIn("user_confirmation", result)
        self.assertEqual(
            workflow.resume(self.root, "sample")["remaining_milestones"],
            ["实现新行为", "回归"],
        )

    def test_rework_retains_selected_context_and_does_not_reuse_withdrawn_ids(self):
        workflow.intake(self.root, "sample", "请求", "范围", ["保留条件", "撤回条件"])
        workflow.map_ac(
            self.root,
            "sample",
            "AC1",
            "原始来源",
            tests=["test_example.ExampleTests.test_behavior"],
        )
        workflow.progress(
            self.root, "sample", "继续", decision="仍有效的约定", gap="旧缺口"
        )
        result = workflow.rework(
            self.root,
            "sample",
            "收窄需求",
            "新范围",
            "用户纠正",
            ["AC1=保留条件"],
            retain_decisions=True,
        )
        self.assertEqual(result["decisions"], ["仍有效的约定"])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(
            result["acceptance"][0]["tests"],
            ["test_example.ExampleTests.test_behavior"],
        )
        self.assertEqual(
            result["history"][-1]["acceptance_changes"]["AC2"], "withdrawn"
        )
        with self.assertRaises(ValueError):
            workflow.rework(
                self.root, "sample", "请求", "范围", "来源", ["AC2=另一个条件"]
            )
        self.assertEqual(workflow.resume(self.root, "sample"), result)
        added = workflow.rework(
            self.root, "sample", "请求", "范围", "来源", ["AC1=保留条件", "AC3=新条件"]
        )
        self.assertEqual(added["acceptance"][-1]["id"], "AC3")

    def test_progress_clears_old_evidence_and_can_replace_or_clear_milestones(self):
        self.start()
        self.verified()
        workflow.complete(self.root, "sample", "已完成", ["AC1=旧证据"])
        result = workflow.progress(
            self.root, "sample", "继续", milestones=["待办一", "待办二"]
        )
        self.assertEqual(result["acceptance"][0]["evidence"], "")
        self.assertEqual(result["acceptance"][0]["verification"]["status"], "pending")
        self.assertEqual(
            workflow.progress(self.root, "sample", "继续")["remaining_milestones"],
            ["待办一", "待办二"],
        )
        self.assertEqual(
            workflow.progress(self.root, "sample", "结束", milestones=[])[
                "remaining_milestones"
            ],
            [],
        )

    def test_targeted_run_must_execute_every_mapped_case(self):
        self.start()
        workflow.map_ac(
            self.root,
            "sample",
            "AC1",
            "需求",
            tests=[
                "test_example.ExampleTests.test_behavior",
                "test_other.OtherTests.test_boundary",
            ],
        )
        code, calls = self.run_check(["--record", "sample", "--test", "test_example"])
        self.assertEqual((code, calls), (1, 2))
        record = workflow.resume(self.root, "sample")
        self.assertEqual(record["acceptance"][0]["verification"]["status"], "not_run")
        self.assertIn(
            "test_other.OtherTests.test_boundary", record["verification"]["errors"][0]
        )
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=测试退出码为零"])

    def test_skipped_mapped_case_is_not_a_pass(self):
        self.start()
        self.verified()
        code, _ = self.run_check(
            ["--record", "sample"],
            cases=[
                {
                    "id": "test_example.ExampleTests.test_behavior",
                    "status": "skipped",
                    "reason": "需要其他平台",
                }
            ],
        )
        self.assertEqual(code, 1)
        ac = workflow.resume(self.root, "sample")["acceptance"][0]
        self.assertEqual(ac["verification"]["status"], "skipped")
        self.assertIn("需要其他平台", ac["verification"]["gaps"][0])

    def test_unmapped_skips_are_reported_without_claiming_they_passed(self):
        self.start()
        self.verified()
        code, _ = self.run_check(
            ["--record", "sample"],
            cases=[
                {"id": "test_example.ExampleTests.test_behavior", "status": "passed"},
                {"id": "test_other.OtherTests.test_unrelated", "status": "passed"},
                {
                    "id": "test_other.OtherTests.test_platform",
                    "status": "skipped",
                    "reason": "POSIX only",
                },
            ],
        )
        self.assertEqual(code, 0)
        result = workflow.complete(
            self.root, "sample", "完成", ["AC1=映射用例通过，平台用例跳过"]
        )
        self.assertEqual(
            result["verification"]["tests"]["cases"][1]["status"], "skipped"
        )
        self.assertEqual(result["verification"]["tests"]["tests_run"], 3)
        self.assertEqual(len(result["verification"]["tests"]["cases"]), 2)

    def test_manual_check_needs_actual_fresh_observation(self):
        self.start(mode="low")
        workflow.map_ac(self.root, "sample", "AC1", "文档要求", manual="人工核对示例")
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"])[0], 1)
        workflow.observe(self.root, "sample", "AC1", "示例与命令帮助一致")
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"])[0], 0)
        (self.root / "README.md").write_text("changed", encoding="utf-8")
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"])[0], 1)
        self.assertEqual(
            workflow.resume(self.root, "sample")["acceptance"][0]["verification"][
                "status"
            ],
            "not_run",
        )

    def test_mapping_or_fixture_change_invalidates_completion(self):
        self.start()
        fixture = self.root / "src/fixture.json"
        fixture.write_text('{"value": 1}', encoding="utf-8")
        self.verified()
        fixture.write_text('{"value": 2}', encoding="utf-8")
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=旧证据"])
        self.verified()
        record = workflow.load_record(self.root, "sample")
        record["acceptance"][0]["tests"] = ["test_other.OtherTests.test_new"]
        workflow.save_json(workflow.record_path(self.root, "sample"), record)
        with self.assertRaises(ValueError):
            workflow.complete(self.root, "sample", "完成", ["AC1=旧证据"])

    def test_missing_runner_report_cannot_reuse_previous_pass(self):
        self.start()
        self.verified()
        code, calls = self.run_check(["--record", "sample"], write_report=False)
        self.assertEqual((code, calls), (1, 2))
        self.assertEqual(
            workflow.resume(self.root, "sample")["verification"]["status"], "failed"
        )

    def test_docs_only_checks_staged_code_even_if_worktree_is_restored(self):
        subprocess.run(
            ["git", "add", "."], cwd=self.root, check=True, capture_output=True
        )
        self.start(mode="low")
        source = self.root / "src/example.py"
        source.write_text("value = 2\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "src/example.py"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        source.write_text("value = 1\n", encoding="utf-8")
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"]), (1, 0))

    def test_docs_only_allows_unchanged_preexisting_code_and_new_docs(self):
        (self.root / "src/example.py").write_text(
            "already_dirty = True\n", encoding="utf-8"
        )
        self.start(mode="low")
        (self.root / "docs/new.md").write_text("Documented behavior", encoding="utf-8")
        workflow.map_ac(self.root, "sample", "AC1", "文档需求", manual="核对新增文档")
        workflow.observe(self.root, "sample", "AC1", "已核对新增文档")
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"]), (0, 0))

    def test_docs_only_rejects_config_and_code_disguised_as_document(self):
        self.start(mode="low")
        for name in ("pyproject.toml", "requirements.txt", "src/prompt.md"):
            with self.subTest(name=name):
                target = self.root / name
                target.write_text("changed", encoding="utf-8")
                self.assertEqual(
                    self.run_check(["--record", "sample", "--docs-only"]), (1, 0)
                )
                target.unlink()

    def test_docs_only_requires_record_and_original_baseline(self):
        self.assertEqual(self.run_check(["--docs-only"]), (1, 0))
        self.start(mode="low")
        baseline = self.root / ".workflow/baselines/sample.json"
        baseline.unlink()
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"]), (1, 0))

    def test_rework_cannot_reset_baseline_to_hide_code_changes(self):
        original = self.start(mode="low")
        (self.root / "src/example.py").write_text("value = 2\n", encoding="utf-8")
        result = workflow.rework(
            self.root, "sample", "改为文档", "只改文档", "用户纠正", ["AC1=文档条件"]
        )
        self.assertEqual(result["baseline"], original["baseline"])
        self.assertEqual(self.run_check(["--record", "sample", "--docs-only"]), (1, 0))

    def test_v1_history_is_readable_and_upgraded_only_when_edited(self):
        legacy = self.start()
        for name in ("revision", "history", "remaining_milestones", "baseline"):
            legacy.pop(name)
        legacy["version"] = 1
        legacy["acceptance"] = [
            {"id": "AC1", "criterion": "旧条件", "evidence": "旧证据"}
        ]
        legacy.update(
            implementation="completed",
            verification={"status": "passed"},
            result="旧实现",
        )
        workflow.save_json(workflow.record_path(self.root, "sample"), legacy)
        self.assertEqual(workflow.resume(self.root, "sample"), legacy)
        self.assertEqual(check.doctor(self.root), [])
        result = workflow.map_ac(
            self.root,
            "sample",
            "AC1",
            "原需求",
            tests=["test_example.ExampleTests.test_behavior"],
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["history"][0]["previous"], legacy)
        self.assertEqual(result["verification"]["status"], "pending")

    def test_unmapped_or_malformed_mapping_does_not_pass(self):
        self.start()
        self.assertEqual(self.run_check(["--record", "sample"])[0], 1)
        with self.assertRaises(ValueError):
            workflow.map_ac(self.root, "sample", "AC1", "需求", tests=["test_example"])
        with self.assertRaises(ValueError):
            workflow.map_ac(self.root, "sample", "AC1", "需求")

    def test_real_cli_round_trip_and_rework(self):
        # Use the repository's real lint policy, not the machine's user config.
        (self.root / "pyproject.toml").write_bytes(
            (workflow.ROOT / "pyproject.toml").read_bytes()
        )
        for name in ("workflow.py", "check.py", "run_tests.py", "workflow_protocol.py", "workflow_knowledge.py"):
            (self.root / "scripts" / name).write_bytes(
                (workflow.ROOT / "scripts" / name).read_bytes()
            )
        (self.root / "tests").mkdir()
        test_file = self.root / "tests/test_example.py"
        test_file.write_text(
            "import unittest\nfrom example import value\n\nclass ExampleTests(unittest.TestCase):\n    def test_behavior(self):\n        self.assertEqual(value, 1)\n",
            encoding="utf-8",
        )

        def cli(script, *args, expected=0):
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

        cli(
            "workflow.py",
            "intake",
            "cli-task",
            "--request",
            "value is one",
            "--scope",
            "example",
            "--acceptance",
            "value equals one",
        )
        cli(
            "workflow.py",
            "map",
            "cli-task",
            "--ac",
            "AC1",
            "--source",
            "request",
            "--test",
            "test_example.ExampleTests.test_behavior",
        )
        cli("check.py", "--record", "cli-task", "--test", "test_example")
        cli(
            "workflow.py",
            "completion",
            "cli-task",
            "--result",
            "one implemented",
            "--evidence",
            "AC1=real unittest passed",
        )
        cli(
            "workflow.py",
            "rework",
            "cli-task",
            "--request",
            "value is two",
            "--scope",
            "example",
            "--source",
            "user correction",
            "--acceptance",
            "AC1=value equals two",
            "--milestones",
            "implement",
            "verify",
        )
        resumed = json.loads(cli("workflow.py", "resume", "cli-task"))
        self.assertEqual(resumed["acceptance"][0]["tests"], [])
        self.assertEqual(
            resumed["history"][-1]["previous"]["implementation"], "completed"
        )
        self.assertEqual(resumed["remaining_milestones"], ["implement", "verify"])
        (self.root / "src/example.py").write_text("value = 2\n", encoding="utf-8")
        test_file.write_text(
            test_file.read_text(encoding="utf-8").replace("value, 1", "value, 2"),
            encoding="utf-8",
        )
        cli(
            "workflow.py",
            "map",
            "cli-task",
            "--ac",
            "AC1",
            "--source",
            "correction",
            "--test",
            "test_example.ExampleTests.test_behavior",
        )
        cli("check.py", "--record", "cli-task", "--test", "test_example")
        completed = json.loads(
            cli(
                "workflow.py",
                "completion",
                "cli-task",
                "--result",
                "two implemented",
                "--evidence",
                "AC1=updated unittest passed",
            )
        )
        self.assertEqual(completed["acceptance_status"], "pending")
        self.assertEqual(completed["remaining_milestones"], [])

    def test_doctor_rejects_corrupt_recovery_and_observation_data(self):
        original = self.start()
        for field, value in (("history", ["broken"]), ("baseline", "not-a-hash")):
            with self.subTest(field=field):
                record = {**original, field: value}
                workflow.save_json(workflow.record_path(self.root, "sample"), record)
                self.assertTrue(check.doctor(self.root))
        original["acceptance"][0]["manual_result"] = "broken"
        workflow.save_json(workflow.record_path(self.root, "sample"), original)
        self.assertTrue(check.doctor(self.root))


class TestRunnerTests(unittest.TestCase):
    def test_report_records_real_outcomes_and_subtests(self):
        class Examples(unittest.TestCase):
            def test_pass(self):
                self.assertEqual(2 + 2, 4)

            @unittest.skip("platform unavailable")
            def test_skip(self):
                self.fail("must not execute")

            def test_subtest_failure(self):
                with self.subTest(value=1):
                    self.assertEqual(1, 2)

            def test_subtest_skip(self):
                with self.subTest(value=1):
                    self.skipTest("subtest unavailable")

            @unittest.expectedFailure
            def test_expected_failure(self):
                self.fail("known issue")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Examples)
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "results.json"
            with (
                patch.object(run_tests.unittest, "TestLoader") as loader,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                loader.return_value.loadTestsFromNames.return_value = suite
                self.assertEqual(run_tests.main(["examples"], report), 1)
            result = check.read_test_report(report)
        outcomes = {case["id"].rsplit(".", 1)[-1]: case for case in result["cases"]}
        self.assertEqual(result["tests_run"], 5)
        self.assertEqual(outcomes["test_pass"]["status"], "passed")
        self.assertEqual(outcomes["test_skip"]["reason"], "platform unavailable")
        self.assertEqual(outcomes["test_subtest_failure"]["status"], "failed")
        self.assertEqual(outcomes["test_subtest_skip"]["status"], "skipped")
        self.assertEqual(
            outcomes["test_expected_failure"]["status"], "expected_failure"
        )

    def test_empty_suite_is_failure(self):
        with (
            patch.object(run_tests.unittest, "TestLoader") as loader,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            loader.return_value.loadTestsFromNames.return_value = unittest.TestSuite()
            self.assertEqual(run_tests.main(["empty"]), 1)

    def test_invalid_selection_is_failure(self):
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/run_tests.py"),
            "test_module_that_does_not_exist",
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, encoding="utf-8", timeout=30
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stderr)

    def test_runner_cli_writes_report_from_real_test_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "results.json"
            test_id = "test_workflow.HygieneTests.test_nested_and_case_variants_and_database_sidecars"
            command = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts/run_tests.py"),
                "--report",
                str(report),
                test_id,
            ]
            result = subprocess.run(
                command, capture_output=True, encoding="utf-8", timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                check.read_test_report(report)["cases"],
                [{"id": test_id, "status": "passed"}],
            )


if __name__ == "__main__":
    unittest.main()
