"""Local development workflow with optional contracts and progressive context."""

import argparse
import copy
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODES = ("low", "medium", "full")
AC_STATUSES = ("pending", "passed", "failed", "skipped", "not_run", "blocked")


def now():
    return datetime.now(timezone.utc).isoformat()


def intent_fingerprint(record):
    intent = {
        name: record[name] for name in ("request", "scope", "mode", "decisions", "gaps")
    }
    intent["acceptance"] = [
        {
            name: item.get(name)
            for name in ("id", "criterion", "source", "tests", "manual")
        }
        for item in record["acceptance"]
    ]
    intent["revision"] = record.get("revision", 1)
    intent["baseline"] = record.get("baseline")
    if "protocol" in record:
        intent["protocol"] = record["protocol"]
    return hashlib.sha256(
        json.dumps(intent, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def record_path(root, key):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", key):
        raise ValueError("记录 key 只能包含小写字母、数字和连字符，最长 80 字符。")
    path = root / "docs" / "changes" / f"{key}.json"
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("记录路径不能离开项目目录。")
    return path


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_record(record):
    if not isinstance(record, dict):
        raise ValueError("记录必须是 JSON 对象。")
    if record.get("version") not in (1, 2) or record.get("mode") not in MODES:
        raise ValueError("记录版本或工作流档位无效。")
    for name in ("key", "request", "scope", "next_action"):
        if not isinstance(record.get(name), str) or not record[name].strip():
            raise ValueError(f"记录缺少 {name}。")
    for name in ("decisions", "gaps"):
        if not isinstance(record.get(name), list) or any(
            not isinstance(value, str) for value in record[name]
        ):
            raise ValueError(f"{name} 必须是字符串列表。")
    verification = record.get("verification")
    if not isinstance(verification, dict) or verification.get("status") not in (
        "pending",
        "passed",
        "failed",
    ):
        raise ValueError("验证状态无效。")
    criteria = record.get("acceptance")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("记录必须包含可观察的验收条件。")
    ids = set()
    for item in criteria:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not re.fullmatch(r"AC[1-9][0-9]*", item["id"])
            or not isinstance(item.get("criterion"), str)
            or not item["criterion"].strip()
        ):
            raise ValueError("验收条件缺少 id 或 criterion。")
        if item["id"] in ids:
            raise ValueError("验收条件 id 重复。")
        ids.add(item["id"])
        if record["version"] == 2:
            if not isinstance(item.get("source"), str) or not item["source"].strip():
                raise ValueError("AC 必须有需求来源。")
            tests = item.get("tests")
            if (
                not isinstance(tests, list)
                or any(
                    not isinstance(test, str)
                    or not re.fullmatch(r"test_\w+(?:\.\w+){2,}", test)
                    for test in tests
                )
                or len(set(tests)) != len(tests)
            ):
                raise ValueError(
                    "AC tests 必须是唯一的完整 unittest 用例 ID（模块.类.方法）。"
                )
            if not isinstance(item.get("manual"), str):
                raise ValueError("AC manual 必须是检查方法文本。")
            if (
                not isinstance(item.get("verification"), dict)
                or item["verification"].get("status") not in AC_STATUSES
            ):
                raise ValueError("AC 验证状态无效。")
            if "manual_result" in item:
                observation = item["manual_result"]
                if (
                    not isinstance(observation, dict)
                    or observation.get("status") not in ("passed", "failed", "blocked")
                    or any(
                        not isinstance(observation.get(name), str)
                        or not observation[name].strip()
                        for name in (
                            "evidence",
                            "at",
                            "fingerprint",
                            "intent_fingerprint",
                        )
                    )
                ):
                    raise ValueError("人工检查结果缺少状态、证据或输入指纹。")
    if record["version"] == 2:
        if not isinstance(record.get("revision"), int) or record["revision"] < 1:
            raise ValueError("任务修订号无效。")
        if not isinstance(record.get("history"), list):
            raise ValueError("返工历史必须是列表。")
        for event in record["history"]:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("source"), str)
                or not event["source"].strip()
                or not isinstance(event.get("previous"), dict)
            ):
                raise ValueError("返工历史缺少来源或上一版约定。")
            prior = event["previous"].get("acceptance")
            if (
                not isinstance(prior, list)
                or not prior
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"AC[1-9][0-9]*", item["id"])
                    for item in prior
                )
            ):
                raise ValueError("返工历史的 AC 编号无效。")
        if "baseline" in record and (
            not isinstance(record["baseline"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["baseline"])
        ):
            raise ValueError("任务基线指纹无效。")
        milestones = record.get("remaining_milestones")
        if not isinstance(milestones, list) or any(
            not isinstance(value, str) or not value.strip() for value in milestones
        ):
            raise ValueError("剩余里程碑必须是非空文本列表。")
    if record.get("implementation") not in ("in_progress", "completed"):
        raise ValueError("实现状态无效。")
    if record.get("acceptance_status") not in ("pending", "accepted"):
        raise ValueError("用户验收状态无效。")
    if record["acceptance_status"] == "accepted" and not record.get(
        "user_confirmation"
    ):
        raise ValueError("用户验收必须有明确确认的来源，自动测试不能代替。")
    if record["implementation"] == "completed":
        if record.get("verification", {}).get("status") != "passed":
            raise ValueError("完成记录必须包含通过的检查。")
        if not record.get("result") or any(
            not item.get("evidence") for item in criteria
        ):
            raise ValueError("完成记录必须有交付结果及逐项验收证据。")
        if record["version"] == 2 and any(
            item["verification"]["status"] != "passed"
            or not (item["tests"] or item["manual"])
            for item in criteria
        ):
            raise ValueError("完成记录的每条 AC 必须有实际通过的映射验证。")
    if "protocol" in record:
        from workflow_protocol import validate

        validate(record["protocol"], ids)


def criterion(key, text, source):
    return dict(
        id=key,
        criterion=text,
        source=source,
        tests=[],
        manual="",
        evidence="",
        verification={"status": "pending", "gaps": []},
    )


def invalidate(record):
    record.update(
        implementation="in_progress",
        verification={"status": "pending"},
        acceptance_status="pending",
        result="",
        updated_at=now(),
    )
    record.pop("user_confirmation", None)
    for item in record["acceptance"]:
        item.update(evidence="", verification={"status": "pending", "gaps": []})
        item.pop("manual_result", None)


def upgrade(record):
    """Read v1 unchanged; upgrade only when actively editing/verifying that task."""
    if record["version"] == 1:
        previous = copy.deepcopy(record)
        record.update(
            version=2,
            revision=1,
            remaining_milestones=[],
            history=[{"at": now(), "source": "v1 记录升级", "previous": previous}],
        )
        for item in record["acceptance"]:
            item.setdefault("source", record["request"])
            item.setdefault("tests", [])
            item.setdefault("manual", "")
        invalidate(record)
    return record


def load_record(root, key):
    record = json.loads(record_path(root, key).read_text(encoding="utf-8"))
    validate_record(record)
    if record["key"] != key:
        raise ValueError("记录 key 与文件名不一致。")
    return record


def intake(root, key, request, scope, acceptance, mode="medium"):
    from check import workspace_snapshot, snapshot_digest

    path = record_path(root, key)
    if path.exists():
        raise ValueError("记录已存在，请 resume 或 progress，禁止覆盖原始请求。")
    record = dict(
        version=2,
        revision=1,
        history=[],
        remaining_milestones=[],
        key=key,
        mode=mode,
        request=request,
        scope=scope,
        acceptance=[
            criterion(f"AC{i}", text, request) for i, text in enumerate(acceptance, 1)
        ],
        decisions=[],
        gaps=[],
        next_action="读取相关代码并确定验证基线",
        implementation="in_progress",
        verification={"status": "pending"},
        acceptance_status="pending",
        result="",
        updated_at=now(),
    )
    validate_record(record)
    snapshot = workspace_snapshot(root)
    record["baseline"] = snapshot_digest(snapshot)
    save_json(root / ".workflow" / "baselines" / f"{key}.json", snapshot)
    save_json(path, record)
    return record


def resume(root, key=None):
    if key:
        return load_record(root, key)
    active = [
        load_record(root, path.stem)
        for path in sorted((root / "docs/changes").glob("*.json"))
    ]
    active = [
        item
        for item in active
        if item["implementation"] != "completed"
        and item.get("protocol", {}).get("state", "active") == "active"
    ]
    if len(active) != 1:
        raise ValueError(
            "活动任务不是唯一一个，请显式提供 key；候选："
            + ", ".join(item["key"] for item in active)
        )
    return active[0]


def progress(root, key, next_action, decision=None, gap=None, milestones=None):
    from workflow_protocol import ensure_running

    record = upgrade(load_record(root, key))
    ensure_running(record)
    invalidate(record)
    record["next_action"] = next_action
    if milestones is not None:
        record["remaining_milestones"] = milestones
    for field, value in (("decisions", decision), ("gaps", gap)):
        if value and value not in record[field]:
            record[field].append(value)
    validate_record(record)
    save_json(record_path(root, key), record)
    return record


def rework(
    root,
    key,
    request,
    scope,
    source,
    acceptance,
    retain_decisions=False,
    retain_gaps=False,
    milestones=None,
    mode=None,
):
    if not all(value.strip() for value in (request, scope, source)):
        raise ValueError("返工必须提供当前请求、范围和变更来源。")
    record = upgrade(load_record(root, key))
    from workflow_protocol import ensure_running, on_rework

    ensure_running(record)
    previous = copy.deepcopy(
        {name: value for name, value in record.items() if name != "history"}
    )
    old = {item["id"]: item for item in record["acceptance"]}
    historical_ids = set(old)
    for event in record["history"]:
        historical_ids.update(item["id"] for item in event["previous"]["acceptance"])
    highest = max(int(value[2:]) for value in historical_ids)
    replacements, dispositions = [], {}
    for value in acceptance:
        ac_id, separator, text = value.partition("=")
        if (
            not separator
            or not re.fullmatch(r"AC[1-9][0-9]*", ac_id)
            or not text.strip()
            or ac_id in dispositions
        ):
            raise ValueError("返工验收格式为 AC1=当前条件，编号不可重复。")
        if ac_id not in old and int(ac_id[2:]) <= highest:
            raise ValueError("已撤回的 AC 编号不能复用，新条件使用更大的编号。")
        retained = ac_id in old and old[ac_id]["criterion"] == text.strip()
        replacements.append(
            copy.deepcopy(old[ac_id])
            if retained
            else criterion(ac_id, text.strip(), source)
        )
        dispositions[ac_id] = (
            "retained" if retained else "replaced" if ac_id in old else "added"
        )
    dispositions.update(
        {ac_id: "withdrawn" for ac_id in old if ac_id not in dispositions}
    )
    record["history"].append(
        dict(
            at=now(),
            source=source,
            previous=previous,
            acceptance_changes=dispositions,
            context_review={
                "decisions": "retained" if retain_decisions else "pending_review",
                "gaps": "retained" if retain_gaps else "pending_review",
            },
        )
    )
    record.update(
        request=request,
        scope=scope,
        revision=record["revision"] + 1,
        acceptance=replacements,
        mode=mode or record["mode"],
        decisions=record["decisions"] if retain_decisions else [],
        gaps=record["gaps"] if retain_gaps else [],
        remaining_milestones=milestones or [],
        next_action="核对返工范围并更新 AC 验证映射",
    )
    on_rework(record)
    invalidate(record)
    validate_record(record)
    save_json(record_path(root, key), record)
    return record


def find_ac(record, ac):
    for item in record["acceptance"]:
        if item["id"] == ac:
            return item
    raise ValueError(f"没有验收条件：{ac}")


def map_ac(root, key, ac, source, tests=None, manual=""):
    from workflow_protocol import ensure_running

    if not (tests or manual.strip()):
        raise ValueError("AC 至少需要一个测试 ID 或明确的人工检查方法。")
    record = upgrade(load_record(root, key))
    ensure_running(record)
    item = find_ac(record, ac)
    item.update(source=source, tests=tests or [], manual=manual)
    invalidate(record)
    validate_record(record)
    save_json(record_path(root, key), record)
    return record


def observe(root, key, ac, evidence, status="passed"):
    from check import fingerprint
    from workflow_protocol import ensure_running

    record = upgrade(load_record(root, key))
    ensure_running(record)
    item = find_ac(record, ac)
    if (
        not item["manual"]
        or not evidence.strip()
        or status not in ("passed", "failed", "blocked")
    ):
        raise ValueError("人工结果需要先 map --manual，提供实际观察证据和有效状态。")
    item["manual_result"] = dict(
        status=status,
        evidence=evidence,
        at=now(),
        fingerprint=fingerprint(root),
        intent_fingerprint=intent_fingerprint(record),
    )
    # New observations require re-evaluation; do not retain a prior green completion.
    record.update(
        implementation="in_progress",
        verification={"status": "pending"},
        acceptance_status="pending",
        result="",
        updated_at=now(),
    )
    record.pop("user_confirmation", None)
    item.update(evidence="", verification={"status": "pending", "gaps": []})
    save_json(record_path(root, key), record)
    return record


def evaluate_acceptance(record, report):
    """Only actual case outcomes can satisfy automated mappings; skips are not passes."""
    cases = {case["id"]: case for case in report.get("tests", {}).get("cases", [])}
    errors = []
    rank = {
        "passed": 0,
        "skipped": 1,
        "not_run": 2,
        "pending": 3,
        "blocked": 4,
        "failed": 5,
    }
    for item in record["acceptance"]:
        statuses, gaps = [], []
        for test in item["tests"]:
            case = cases.get(test, {"status": "not_run"})
            status = case["status"]
            if status not in rank:
                status = "failed"
            statuses.append(status)
            if status != "passed":
                gaps.append(
                    f"{test}: {status}"
                    + (f" ({case['reason']})" if case.get("reason") else "")
                )
        if item["manual"]:
            result = item.get("manual_result", {})
            fresh = result.get("fingerprint") == report.get(
                "fingerprint"
            ) and result.get("intent_fingerprint") == intent_fingerprint(record)
            status = result.get("status", "not_run") if fresh else "not_run"
            if status not in rank or not result.get("evidence"):
                status = "not_run"
            statuses.append(status)
            if status != "passed":
                gaps.append(f"人工检查：{status}，缺少当前输入的通过证据")
        if not statuses:
            statuses, gaps = ["pending"], ["未映射测试或人工检查"]
        status = max(statuses, key=rank.__getitem__)
        item["verification"] = {"status": status, "gaps": gaps}
        if status != "passed":
            errors.append(f"{item['id']} 未通过：{'；'.join(gaps)}")
    return errors


def complete(root, key, result, evidence):
    from check import doctor, fingerprint
    from workflow_protocol import readiness

    record = load_record(root, key)
    gaps = readiness(root, record)
    if gaps:
        raise ValueError("；".join(gaps))
    if record["version"] != 2:
        raise ValueError("旧记录仍可读取；继续交付前请先 map 验收条件。")
    if doctor(root):
        raise ValueError("当前仓库 doctor 未通过，请检查索引、上下文入口和记录。")
    verification = record["verification"]
    if (
        verification.get("status") != "passed"
        or verification.get("fingerprint") != fingerprint(root)
        or verification.get("intent_fingerprint") != intent_fingerprint(record)
    ):
        raise ValueError("尚无当前代码的通过证据，请运行 check.py --record KEY。")
    if record["mode"] == "full" and verification.get("scope") != "full":
        raise ValueError("full 档位必须完成全量检查。")
    from check import docs_scope_errors

    if verification.get("scope") == "doctor" and docs_scope_errors(root, record):
        raise ValueError("当前变更不满足仅文档验证范围。")
    if evaluate_acceptance(record, verification):
        raise ValueError("尚有 AC 未通过，检查映射测试、跳过项及人工证据。")
    by_id = {}
    for value in evidence:
        key_id, separator, text = value.partition("=")
        if not separator or not text.strip() or key_id in by_id:
            raise ValueError("证据格式为 AC1=实际验证结果，每项只提供一次。")
        by_id[key_id] = text.strip()
    if set(by_id) != {item["id"] for item in record["acceptance"]}:
        raise ValueError("请为每条验收条件提供对应证据，不能遗漏或虚构编号。")
    for item in record["acceptance"]:
        item["evidence"] = by_id[item["id"]]
    record.update(
        implementation="completed",
        result=result,
        next_action="等待用户验收",
        remaining_milestones=[],
        acceptance_status="pending",
        updated_at=now(),
    )
    validate_record(record)
    save_json(record_path(root, key), record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("intake")
    start.add_argument("key")
    start.add_argument("--request", required=True)
    start.add_argument("--scope", required=True)
    start.add_argument("--acceptance", action="append", required=True)
    start.add_argument("--mode", choices=MODES, default="medium")
    step = commands.add_parser("progress")
    step.add_argument("key")
    step.add_argument("--next", dest="next_action", required=True)
    step.add_argument("--decision")
    step.add_argument("--gap")
    step.add_argument("--milestones", nargs="*")
    revise = commands.add_parser("rework")
    revise.add_argument("key")
    for name in ("request", "scope", "source"):
        revise.add_argument(f"--{name}", required=True)
    revise.add_argument("--acceptance", action="append", required=True)
    revise.add_argument("--retain-decisions", action="store_true")
    revise.add_argument("--retain-gaps", action="store_true")
    revise.add_argument("--milestones", nargs="*")
    revise.add_argument("--mode", choices=MODES)
    mapping = commands.add_parser("map")
    mapping.add_argument("key")
    mapping.add_argument("--ac", required=True)
    mapping.add_argument("--source", required=True)
    mapping.add_argument("--test", dest="tests", action="append")
    mapping.add_argument("--manual", default="")
    observation = commands.add_parser("observe")
    observation.add_argument("key")
    observation.add_argument("--ac", required=True)
    observation.add_argument("--evidence", required=True)
    observation.add_argument(
        "--status", choices=("passed", "failed", "blocked"), default="passed"
    )
    restore = commands.add_parser("resume")
    restore.add_argument("key", nargs="?")
    detail = restore.add_mutually_exclusive_group()
    detail.add_argument(
        "--full", action="store_true", help="显式读取完整历史；旧任务保持原默认输出"
    )
    detail.add_argument(
        "--summary", action="store_true", help="仅当前状态；协议任务默认使用摘要"
    )
    finish = commands.add_parser("completion")
    finish.add_argument("key")
    finish.add_argument("--result", required=True)
    finish.add_argument("--evidence", action="append", required=True)
    commands.add_parser("doctor")
    from workflow_protocol import register as register_protocol, summary
    from workflow_knowledge import register as register_knowledge

    extensions = {**register_protocol(commands), **register_knowledge(commands)}
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "doctor":
            from check import doctor

            errors = doctor(ROOT)
            print("\n".join(errors) if errors else "Workflow doctor: passed")
            return bool(errors)
        if command == "resume":
            record = resume(ROOT, args["key"])
            value = (
                summary(ROOT, record["key"])
                if args["summary"] or ("protocol" in record and not args["full"])
                else record
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        action = {
            "intake": intake,
            "progress": progress,
            "rework": rework,
            "map": map_ac,
            "observe": observe,
            "resume": resume,
            "completion": complete,
            **extensions,
        }[command]
        print(json.dumps(action(ROOT, **args), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"工作流未完成：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
