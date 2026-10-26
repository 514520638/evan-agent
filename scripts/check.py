"""One local/CI verification entrypoint. Reports contain commands and exit codes."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
import tempfile

from workflow import (
    ROOT,
    evaluate_acceptance,
    intent_fingerprint,
    load_record,
    now,
    record_path,
    save_json,
    upgrade,
)

REQUIRED = (
    "AGENTS.md",
    "docs/DEVELOPMENT.md",
    "docs/ARCHITECTURE.md",
    "docs/changes/README.md",
    "scripts/workflow.py",
    "scripts/check.py",
    "scripts/run_tests.py",
    "scripts/workflow_protocol.py",
    "scripts/workflow_knowledge.py",
    ".github/workflows/ci.yml",
    ".github/pull_request_template.md",
)


def sensitive_paths(paths):
    """Check names only, including nested environments and database backups."""
    result = []
    for path in paths:
        parts = PurePosixPath(path.replace("\\", "/")).parts
        name = parts[-1].lower()
        if (
            any(part.lower() in (".local", ".workflow", ".venv") for part in parts)
            or name == ".env"
            or (name.startswith(".env.") and name != ".env.example")
            or re.search(r"\.(sqlite3?|db)(-(wal|shm|journal))?$", name)
        ):
            result.append(path)
    return result


def git(root, *args, input=None):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=input,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    ).stdout


def workspace_snapshot(root):
    """Public Git inputs only; include index changes and non-ignored new files."""
    indexed = {}
    for entry in filter(None, git(root, "ls-files", "--stage", "-z").split("\0")):
        metadata, name = entry.split("\t", 1)
        indexed.setdefault(name, []).append(metadata)
    names = set(indexed) | set(
        filter(
            None,
            git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"),
        )
    )
    snapshot = {}
    for name in sorted(names):
        if sensitive_paths([name]) or (
            name.startswith("docs/changes/") and name.endswith(".json")
        ):
            continue
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"验证输入不能是符号链接或离开仓库：{name}")
        if path.exists() and not path.is_file():
            raise ValueError(f"不支持的验证输入：{name}")
        digest = (
            hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            if path.is_file()
            else None
        )
        snapshot[name] = {"worktree": digest, "index": indexed.get(name, [])}
    return snapshot


def snapshot_digest(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def fingerprint(root):
    return snapshot_digest(workspace_snapshot(root))


def docs_scope_errors(root, record):
    if not record or record["mode"] != "low":
        return ["--docs-only 必须关联 low 任务记录。"]
    baseline_path = root / ".workflow" / "baselines" / f"{record['key']}.json"
    if not record.get("baseline") or not baseline_path.is_file():
        return ["缺少任务开始时的本地基线，不能证明只有文档变更；请运行完整 check。"]
    if baseline_path.is_symlink() or not baseline_path.resolve().is_relative_to(
        root.resolve()
    ):
        return ["本地基线路径无效。"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if (
        not isinstance(baseline, dict)
        or snapshot_digest(baseline) != record["baseline"]
    ):
        return ["本地基线不匹配，不能使用仅文档检查。"]
    current = workspace_snapshot(root)
    changed = sorted(
        name
        for name in set(baseline) | set(current)
        if baseline.get(name) != current.get(name)
    )
    # Only conventional prose locations qualify. Unknown files fail closed.
    non_docs = [
        name
        for name in changed
        if not (
            PurePosixPath(name).suffix.lower() in (".md", ".rst")
            and (
                "/" not in name
                or name.startswith("docs/")
                or name == ".github/pull_request_template.md"
            )
        )
    ]
    return [f"仅文档检查发现代码、配置或未知文件变更：{name}" for name in non_docs]


def read_test_report(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("tests_run"), int)
        or value["tests_run"] <= 0
    ):
        raise ValueError("测试报告没有实际执行用例。")
    cases = value.get("cases")
    allowed = (
        "passed",
        "failed",
        "error",
        "skipped",
        "expected_failure",
        "unexpected_success",
    )
    if (
        not isinstance(cases, list)
        or not cases
        or any(
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or case.get("status") not in allowed
            for case in cases
        )
        or len({case["id"] for case in cases}) != len(cases)
    ):
        raise ValueError("测试报告缺少有效的逐用例结果。")
    return value


def doctor(root):
    errors = [
        f"缺少工作流文件：{name}" for name in REQUIRED if not (root / name).is_file()
    ]
    try:
        tracked = git(root, "ls-files", "-z").split("\0")
        errors.extend(
            f"禁止跟踪本地敏感路径：{name}"
            for name in sensitive_paths(filter(None, tracked))
        )
        probes = [
            ".env",
            ".env.production",
            ".local/probe",
            ".workflow/check.json",
            "backup.sqlite3",
        ]
        ignored = set(
            git(
                root,
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
                input="\0".join(probes) + "\0",
            ).split("\0")
        )
        errors.extend(f"缺少忽略规则：{name}" for name in probes if name not in ignored)
    except (OSError, subprocess.CalledProcessError):
        errors.append("无法检查 Git 索引／忽略规则；必须在有效 Git 仓库中运行。")
    for path in (root / "docs/changes").glob("*.json"):
        try:
            load_record(root, path.stem)
        except (ValueError, OSError, KeyError, TypeError) as error:
            errors.append(f"无效任务记录 {path.name}：{error}")
    # Validate the small context map, not every historical document or external URL.
    for name in ("AGENTS.md", "docs/DEVELOPMENT.md", "docs/ARCHITECTURE.md"):
        path = root / name
        if not path.is_file():
            continue
        for target in re.findall(
            r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")
        ):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#")[0]
            if target and not (path.parent / target).exists():
                errors.append(f"上下文链接失效：{name} -> {target}")
    from workflow_knowledge import doctor as knowledge_doctor

    errors.extend(knowledge_doctor(root))
    return errors


def run_step(root, name, command):
    print(f"\n[{name}] {' '.join(command)}", flush=True)
    started = time.monotonic()
    try:
        code = subprocess.run(
            command,
            cwd=root,
            timeout=300,
            check=False,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        ).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"检查无法执行：{type(error).__name__}", flush=True)
        code = 1
    return {
        "name": name,
        "command": [
            "python" if value == sys.executable else value for value in command
        ],
        "exit_code": code,
        "seconds": round(time.monotonic() - started, 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", help="将实际检查结果写回指定任务")
    parser.add_argument(
        "--test", nargs="+", help="聚焦测试模块，例如 test_auth test_sessions"
    )
    parser.add_argument(
        "--docs-only", action="store_true", help="low 文档任务只运行 doctor"
    )
    args = parser.parse_args()
    try:
        record = upgrade(load_record(ROOT, args.record)) if args.record else None
        if args.docs_only and (args.test or (record and record["mode"] != "low")):
            raise ValueError("仅文档检查只适用于 low 档位，不能和 --test 一起使用。")
        if record and record["mode"] == "full" and args.test:
            raise ValueError("full 档位不能只运行聚焦测试。")
        initial = fingerprint(ROOT)
        errors = doctor(ROOT)
        if record:
            from workflow_protocol import readiness

            errors.extend(readiness(ROOT, record))
        if args.docs_only:
            errors.extend(docs_scope_errors(ROOT, record))
        report = {
            "status": "failed",
            "scope": "doctor"
            if args.docs_only
            else "targeted"
            if args.test
            else "full",
            "at": now(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "fingerprint": initial,
            "errors": errors,
            "checks": [],
        }
        if record:
            report["intent_fingerprint"] = intent_fingerprint(record)
        if not errors:
            reports_dir = ROOT / ".workflow"
            reports_dir.mkdir(exist_ok=True)
            descriptor, report_name = tempfile.mkstemp(
                prefix="tests-", suffix=".json", dir=reports_dir
            )
            os.close(descriptor)
            test_path = Path(report_name)
            commands = [
                (
                    "lint",
                    [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
                ),
                (
                    "tests",
                    [
                        sys.executable,
                        "scripts/run_tests.py",
                        "--report",
                        test_path.relative_to(ROOT).as_posix(),
                        *(args.test or []),
                    ],
                ),
            ]
            if args.docs_only:
                commands = []
            try:
                for name, command in commands:
                    report["checks"].append(run_step(ROOT, name, command))
                if not args.docs_only:
                    try:
                        report["tests"] = read_test_report(test_path)
                        if any(
                            case["status"] in ("failed", "error", "unexpected_success")
                            for case in report["tests"]["cases"]
                        ):
                            errors.append("测试报告包含失败用例。")
                    except (ValueError, OSError, TypeError):
                        errors.append("缺少本次运行的有效测试报告，不能使用历史结果。")
            finally:
                test_path.unlink(missing_ok=True)
            if record:
                errors.extend(evaluate_acceptance(record, report))
            if fingerprint(ROOT) != initial:
                errors.append("验证期间输入发生变化，请重跑受影响检查。")
            if (
                record
                and intent_fingerprint(load_record(ROOT, record["key"]))
                != report["intent_fingerprint"]
            ):
                raise ValueError(
                    "验证期间任务契约发生变化；保留新记录，不回写旧任务状态。"
                )
            if not errors and all(item["exit_code"] == 0 for item in report["checks"]):
                report["status"] = "passed"
        save_json(ROOT / ".workflow/check.json", report)
        if record:
            if errors:
                # Even preflight failures must not leave stale per-AC green states.
                evaluate_acceptance(record, report)
            compact = dict(report)
            if "tests" in report:
                mapped = {
                    test for item in record["acceptance"] for test in item["tests"]
                }
                compact["tests"] = {
                    **report["tests"],
                    "case_details": "mapped_and_nonpassing",
                    "cases": [
                        case
                        for case in report["tests"]["cases"]
                        if case["id"] in mapped or case["status"] != "passed"
                    ],
                }
            record.update(verification=compact, updated_at=now())
            # A new failed run reopens a previously completed task; never retain a green completion.
            if report["status"] != "passed":
                record.update(
                    implementation="in_progress", acceptance_status="pending", result=""
                )
                record.pop("user_confirmation", None)
                for item in record["acceptance"]:
                    item["evidence"] = ""
            save_json(record_path(ROOT, args.record), record)
        for error in errors:
            print(error)
        print(f"\nChecks: {report['status']} ({report['scope']}); .workflow/check.json")
        return int(report["status"] != "passed")
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"检查未完成：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
