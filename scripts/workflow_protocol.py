"""Optional, versioned task protocol. Classification is supplied by the operator."""

import copy
import re

KINDS = (
    "new",
    "analysis",
    "question",
    "status",
    "supplement",
    "correction",
    "rework",
    "pause",
    "continue",
    "cancel",
)


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空文本。")


def strings(value, name, nonempty=True):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} 必须是文本列表。")
    for item in value:
        text(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name} 不能重复。")


def validate(protocol, ac_ids):
    if not isinstance(protocol, dict) or protocol.get("version") != 1:
        raise ValueError("任务 protocol 版本无效。")
    if protocol.get("state") not in ("active", "paused", "cancelled"):
        raise ValueError("任务生命周期无效。")
    auth = protocol.get("authorization")
    if not isinstance(auth, dict) or auth.get("status") not in (
        "recorded",
        "needs_review",
    ):
        raise ValueError("授权状态无效。")
    text(auth.get("source"), "授权来源")
    strings(auth.get("allowed"), "允许改变")
    strings(auth.get("preserved"), "必须保留")
    if type(auth.get("revision")) is not int or auth["revision"] < 1:
        raise ValueError("授权修订号无效。")
    events = protocol.get("events")
    if not isinstance(events, list):
        raise ValueError("请求事件必须是列表。")
    ids = set()
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in (
            *KINDS,
            "align",
            "plan",
            "link",
            "exempt",
        ):
            raise ValueError("请求事件类型无效。")
        for field in ("id", "source", "message", "at"):
            text(event.get(field), f"事件 {field}")
        if event["id"] in ids:
            raise ValueError("请求事件 ID 重复。")
        ids.add(event["id"])
    pending = protocol.get("pending")
    if not isinstance(pending, list):
        raise ValueError("待对齐请求无效。")
    for item in pending:
        if (
            not isinstance(item, dict)
            or item.get("event") not in ids
            or type(item.get("revision")) is not int
        ):
            raise ValueError("待对齐请求缺少事件和契约修订号。")
    links = protocol.get("stories")
    if not isinstance(links, list):
        raise ValueError("故事线关联必须是列表。")
    seen = set()
    for link in links:
        if not isinstance(link, dict) or link.get("ac") not in ac_ids:
            raise ValueError("故事线关联了无效 AC。")
        for field in ("story", "scenario"):
            if not isinstance(link.get(field), str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9-]{0,79}", link[field]
            ):
                raise ValueError("故事线／场景 ID 无效。")
        if (
            type(link.get("revision")) is not int
            or link["revision"] < 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(link.get("digest", "")))
        ):
            raise ValueError("故事线关联缺少版本和内容指纹。")
        identity = (link["ac"], link["story"], link["scenario"])
        if identity in seen:
            raise ValueError("故事线关联重复。")
        seen.add(identity)
    if not isinstance(protocol.get("story_exemption"), str):
        raise ValueError("故事线不适用理由必须是文本。")
    plan = protocol.get("plan")
    if plan is not None:
        if not isinstance(plan, dict):
            raise ValueError("最小实现方案无效。")
        for field in ("reuse", "changes", "preserve", "verification", "rollback"):
            text(plan.get(field), f"方案 {field}")
        strings(plan.get("steps"), "可验证切片")
        if (
            type(plan.get("revision")) is not int
            or type(plan.get("authorization_revision")) is not int
        ):
            raise ValueError("方案缺少任务／授权版本。")


def ensure_running(record):
    if record.get("protocol", {}).get("state", "active") != "active":
        raise ValueError(
            "任务已暂停或取消；暂停任务需有用户继续来源，取消任务应新建 key。"
        )


def event(protocol, kind, message, source, event_id=None, **details):
    from workflow import now

    for value, name in ((message, "请求摘要"), (source, "请求来源")):
        text(value, name)
    if event_id is None:
        ordinal = len(protocol["events"]) + 1
        while any(item["id"] == f"event-{ordinal}" for item in protocol["events"]):
            ordinal += 1
        event_id = f"event-{ordinal}"
    text(event_id, "事件 ID")
    if any(item["id"] == event_id for item in protocol["events"]):
        raise ValueError("事件 ID 已存在。")
    protocol["events"].append(
        dict(
            id=event_id, kind=kind, message=message, source=source, at=now(), **details
        )
    )
    return event_id


def write(root, record):
    from workflow import invalidate, validate_record, save_json, record_path

    invalidate(record)
    validate_record(record)
    save_json(record_path(root, record["key"]), record)
    return record


def aligned_record(root, key):
    from workflow import load_record

    record = load_record(root, key)
    ensure_running(record)
    if "protocol" not in record:
        raise ValueError("先用 align 记录实际请求来源、允许改变和必须保留的范围。")
    return record


def align(root, key, source, allowed, preserved):
    from workflow import load_record, upgrade

    record = upgrade(load_record(root, key))
    ensure_running(record)
    text(source, "授权来源")
    strings(allowed, "允许改变")
    strings(preserved, "必须保留")
    protocol = record.setdefault(
        "protocol",
        dict(
            version=1,
            state="active",
            events=[],
            pending=[],
            stories=[],
            story_exemption="",
            plan=None,
        ),
    )
    if any(item["revision"] >= record["revision"] for item in protocol["pending"]):
        raise ValueError(
            "待对齐的需求先用 rework 重列当前请求、范围及全部 AC，再 align。"
        )
    previous = copy.deepcopy(protocol.get("authorization"))
    protocol["authorization"] = dict(
        source=source,
        allowed=allowed,
        preserved=preserved,
        status="recorded",
        revision=(previous or {}).get("revision", 0) + 1,
    )
    protocol.update(plan=None, pending=[])
    event(
        protocol,
        "align",
        "核对授权边界；实现细节在已授权范围内自主选择",
        source,
        previous=previous,
        current=copy.deepcopy(protocol["authorization"]),
    )
    return write(root, record)


def request(root, kind, message, source, key=None, event_id=None):
    from workflow import resume

    if kind not in KINDS:
        raise ValueError("未知请求类型。")
    text(message, "请求摘要")
    text(source, "请求来源")
    if kind in ("new", "analysis"):
        return dict(
            kind=kind,
            action="intake" if kind == "new" else "read_only",
            message=message,
            source=source,
        )
    record = resume(root, key)
    if kind in ("question", "status"):
        continuing = (
            record["implementation"] != "completed"
            and record.get("protocol", {}).get("state", "active") == "active"
        )
        return dict(
            kind=kind,
            action="answer_then_continue" if continuing else "answer_only",
            task=summary(root, record["key"]),
            source=source,
        )
    if "protocol" not in record:
        raise ValueError("旧任务先 align；不会从含糊 scope 自动推导授权。")
    protocol = record["protocol"]
    if event_id:
        prior = next(
            (item for item in protocol["events"] if item["id"] == event_id), None
        )
        if prior:
            if (prior["kind"], prior["message"], prior["source"]) != (
                kind,
                message,
                source,
            ):
                raise ValueError("事件 ID 冲突，不能覆盖原请求。")
            return record  # Retry is a no-op, including after later transitions.
    if protocol["state"] == "cancelled":
        raise ValueError("任务已取消；新授权使用新 key，并在来源中引用旧任务。")
    if record["implementation"] == "completed" and kind in (
        "pause",
        "continue",
        "cancel",
    ):
        raise ValueError("实现已完成；需求变更请使用 rework。")
    prior = {
        name: copy.deepcopy(record[name])
        for name in (
            "request",
            "scope",
            "revision",
            "acceptance",
            "verification",
            "implementation",
            "acceptance_status",
            "result",
            "next_action",
        )
    }
    prior["protocol"] = {
        name: copy.deepcopy(protocol[name])
        for name in ("state", "authorization", "plan")
    }
    identifier = event(protocol, kind, message, source, event_id, previous=prior)
    if kind in ("supplement", "correction", "rework"):
        protocol["pending"].append(dict(event=identifier, revision=record["revision"]))
        protocol["authorization"]["status"] = "needs_review"
        protocol["plan"] = None
        record["next_action"] = "对齐本次变更涉及的契约；未受影响工作可继续"
    else:
        protocol["state"] = {
            "pause": "paused",
            "continue": "active",
            "cancel": "cancelled",
        }[kind]
    return write(root, record)


def on_rework(record):
    protocol = record.get("protocol")
    if protocol:
        protocol["authorization"]["status"] = "needs_review"
        protocol["plan"] = None
        previous = {
            item["id"]: item["criterion"]
            for item in record["history"][-1]["previous"]["acceptance"]
        }
        ids = {
            item["id"]
            for item in record["acceptance"]
            if previous.get(item["id"]) == item["criterion"]
        }
        protocol["stories"] = [
            item for item in protocol["stories"] if item["ac"] in ids
        ]
        protocol["story_exemption"] = ""


def plan(root, key, reuse, changes, preserve, steps, verification, rollback):
    record = aligned_record(root, key)
    protocol = record["protocol"]
    if protocol["authorization"]["status"] != "recorded" or protocol["pending"]:
        raise ValueError("先对齐待处理请求与授权。")
    value = dict(
        reuse=reuse,
        changes=changes,
        preserve=preserve,
        steps=steps,
        verification=verification,
        rollback=rollback,
        revision=record["revision"],
        authorization_revision=protocol["authorization"]["revision"],
    )
    previous = copy.deepcopy(protocol["plan"])
    protocol["plan"] = value
    event(
        protocol,
        "plan",
        changes,
        "根据当前授权制定的实现方案；不构成新增授权",
        previous=previous,
        current=copy.deepcopy(value),
    )
    return write(root, record)


def link(root, key, ac, story, scenario):
    from workflow import find_ac
    from workflow_knowledge import load_asset, digest, freshness

    record = aligned_record(root, key)
    find_ac(record, ac)
    asset = load_asset(root, "story", story)
    if asset["status"] == "draft" or freshness(root, asset):
        raise ValueError("故事线是草案或来源已变化；先核对并更新故事线。")
    if scenario not in {item["id"] for item in asset["scenarios"]}:
        raise ValueError("故事线没有该场景。")
    protocol = record["protocol"]
    value = dict(
        ac=ac,
        story=story,
        scenario=scenario,
        revision=asset["revision"],
        digest=digest(asset),
    )
    protocol["stories"] = [
        item
        for item in protocol["stories"]
        if (item["ac"], item["story"], item["scenario"]) != (ac, story, scenario)
    ] + [value]
    protocol["story_exemption"] = ""
    event(
        protocol,
        "link",
        f"{ac} -> {story}/{scenario}@{asset['revision']}",
        "核对现有业务契约；场景关联不替代测试映射",
    )
    return write(root, record)


def exempt(root, key, reason):
    text(reason, "故事线不适用理由")
    record = aligned_record(root, key)
    protocol = record["protocol"]
    event(
        protocol,
        "exempt",
        reason,
        "本任务不影响业务故事线",
        previous=protocol["stories"],
    )
    protocol.update(stories=[], story_exemption=reason)
    return write(root, record)


def readiness(root, record):
    from workflow_knowledge import load_asset, freshness, digest

    protocol = record.get("protocol")
    if not protocol:
        return []  # v1/v2 compatibility; adopted protocol is never silently removed.
    errors = []
    if protocol["state"] != "active":
        errors.append("任务已暂停或取消，不能验证交付。")
    if protocol["authorization"]["status"] != "recorded" or protocol["pending"]:
        errors.append("请求／授权仍待对齐。")
    value = protocol["plan"]
    if (
        not value
        or value["revision"] != record["revision"]
        or value["authorization_revision"] != protocol["authorization"]["revision"]
    ):
        errors.append("缺少当前任务和授权版本的最小实现方案。")
    if not protocol["story_exemption"]:
        covered = {item["ac"] for item in protocol["stories"]}
        if covered != {item["id"] for item in record["acceptance"]}:
            errors.append("每条 AC 需关联故事线场景；不适用时记录任务级理由。")
    for link in protocol["stories"]:
        try:
            asset = load_asset(root, "story", link["story"])
            if (
                asset["status"] == "draft"
                or asset["revision"] != link["revision"]
                or digest(asset) != link["digest"]
                or freshness(root, asset)
                or link["scenario"] not in {item["id"] for item in asset["scenarios"]}
            ):
                errors.append(f"故事线关联已过期：{link['story']}/{link['scenario']}")
        except (ValueError, OSError, KeyError, TypeError):
            errors.append(f"故事线关联无法读取：{link['story']}")
    return errors


def summary(root, key=None):
    from workflow import resume, intent_fingerprint
    from check import fingerprint

    record = resume(root, key)
    value = {
        name: copy.deepcopy(record[name])
        for name in (
            "key",
            "request",
            "scope",
            "mode",
            "revision",
            "decisions",
            "gaps",
            "next_action",
            "implementation",
            "acceptance_status",
            "result",
        )
        if name in record
    }
    value["remaining_milestones"] = record.get("remaining_milestones", [])
    value["acceptance"] = [
        {
            name: copy.deepcopy(item.get(name))
            for name in ("id", "criterion", "source", "tests", "manual", "verification")
        }
        for item in record["acceptance"]
    ]
    protocol = record.get("protocol")
    value["protocol"] = (
        {
            name: copy.deepcopy(protocol[name])
            for name in (
                "state",
                "authorization",
                "pending",
                "stories",
                "story_exemption",
                "plan",
            )
        }
        if protocol
        else None
    )
    if protocol:
        events = {item["id"]: item for item in protocol["events"]}
        value["protocol"]["pending"] = [
            {
                **item,
                **{
                    name: events[item["event"]][name]
                    for name in ("kind", "message", "source")
                },
            }
            for item in protocol["pending"]
        ]
    report = record["verification"]
    value["verification"] = dict(
        status=report["status"],
        fresh=report.get("fingerprint") == fingerprint(root)
        and report.get("intent_fingerprint") == intent_fingerprint(record),
        scope=report.get("scope"),
    )
    value["readiness_gaps"] = readiness(root, record)
    value["history"] = dict(
        revisions=len(record.get("history", [])),
        events=len((protocol or {}).get("events", [])),
        detail=f"workflow.py resume {record['key']} --full",
    )
    value["diff_reminder"] = "核对当前工作树和暂存区；摘要不能证明既有变更归属。"
    return value


def register(commands):
    parser = commands.add_parser("align", help="记录人工请求的授权来源和边界")
    parser.add_argument("key")
    parser.add_argument("--source", required=True)
    parser.add_argument("--allow", dest="allowed", action="append", required=True)
    parser.add_argument("--preserve", dest="preserved", action="append", required=True)
    parser = commands.add_parser(
        "request", help="依据对话识别请求后，记录类型并路由；不做关键词授权"
    )
    parser.add_argument("key", nargs="?")
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--id", dest="event_id")
    parser = commands.add_parser("plan", help="保存最小可验证实现方案")
    parser.add_argument("key")
    for name in ("reuse", "changes", "preserve", "verification", "rollback"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--step", dest="steps", action="append", required=True)
    parser = commands.add_parser("link", help="关联 AC 与版本化故事线场景")
    parser.add_argument("key")
    for name in ("ac", "story", "scenario"):
        parser.add_argument(f"--{name}", required=True)
    parser = commands.add_parser("exempt", help="记录故事线不适用的具体原因")
    parser.add_argument("key")
    parser.add_argument("--reason", required=True)
    return dict(align=align, request=request, plan=plan, link=link, exempt=exempt)
