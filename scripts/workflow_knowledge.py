"""Versioned local storylines, evidence-backed lessons, and bounded context lookup."""

import copy
import hashlib
import json
from pathlib import PurePosixPath
import re

from workflow_protocol import text, strings

DIRECTORIES = {"story": "docs/storylines", "experience": "docs/experience"}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,79}", value
    ):
        raise ValueError("资产 ID 只能包含小写字母、数字和连字符，最长 80 字符。")
    return value


def public_path(root, name):
    from check import sensitive_paths

    text(name, "来源路径")
    path = PurePosixPath(name)
    reserved = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)
    if (
        "\\" in name
        or ":" in name
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or name != path.as_posix()
        or any(
            part.lower() == ".git" or part.endswith((".", " ")) or reserved.match(part)
            for part in path.parts
        )
        or sensitive_paths([name])
    ):
        raise ValueError(f"只允许仓库内公开相对路径：{name}")
    target = root / name
    # Also reject Windows junctions/reparse aliases, including aliases that stay
    # inside the repository but resolve into a private directory.
    if target.resolve() != root.resolve() / name or any(
        part.is_symlink()
        for part in [target, *target.parents]
        if part != root and part.is_relative_to(root)
    ):
        raise ValueError(f"来源路径不能是链接或离开仓库：{name}")
    return target


def read_public(root, name):
    path = public_path(root, name)
    if not path.is_file() or path.stat().st_size > 512_000:
        raise ValueError(f"来源缺失、不是文件或超过 512 KB：{name}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as error:
        raise ValueError(f"来源必须是 UTF-8 文本：{name}") from error


def content_hash(root, name):
    return hashlib.sha256(
        read_public(root, name).replace("\r\n", "\n").encode()
    ).hexdigest()


def asset_path(root, kind, asset_id):
    if kind not in DIRECTORIES:
        raise ValueError("未知资产类型。")
    return public_path(root, f"{DIRECTORIES[kind]}/{identifier(asset_id)}.json")


def digest(asset):
    # A history append does not change the meaning of the current version.
    current = {
        key: value
        for key, value in asset.items()
        if key not in ("history", "updated_at")
    }
    return hashlib.sha256(
        json.dumps(current, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def validate_asset(asset, kind):
    if (
        not isinstance(asset, dict)
        or asset.get("version") != 1
        or asset.get("kind") != kind
    ):
        raise ValueError("知识资产类型或版本无效。")
    identifier(asset.get("id"))
    for field in ("title", "source"):
        text(asset.get(field), field)
    strings(asset.get("tags"), "检索标签")
    if type(asset.get("revision")) is not int or asset["revision"] < 1:
        raise ValueError("知识资产修订号无效。")
    sources = asset.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("必须有可追溯的公开文件来源。")
    seen = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError("来源必须包含路径和指纹。")
        text(item.get("path"), "来源路径")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
            or item["path"] in seen
        ):
            raise ValueError("来源指纹无效或路径重复。")
        seen.add(item["path"])
    history = asset.get("history")
    if not isinstance(history, list):
        raise ValueError("资产历史必须是列表。")
    for event in history:
        if not isinstance(event, dict) or not isinstance(event.get("previous"), dict):
            raise ValueError("资产历史缺少上一版。")
        text(event.get("source"), "修订来源")
    if kind == "story":
        if asset.get("status") not in ("observed", "confirmed", "draft"):
            raise ValueError("故事线状态必须区分当前观察、已确认目标和草案。")
        for field in ("actors", "preconditions", "invariants"):
            strings(asset.get(field), field)
        scenarios = asset.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("故事线缺少场景。")
        ids = set()
        for item in scenarios:
            if not isinstance(item, dict):
                raise ValueError("场景必须是对象。")
            scenario_id = identifier(item.get("id"))
            if scenario_id in ids:
                raise ValueError("场景 ID 重复。")
            ids.add(scenario_id)
            for field in ("title", "given", "when", "then", "from", "to"):
                text(item.get(field), field)
            strings(item.get("tests"), "场景测试", nonempty=False)
            if any(
                not re.fullmatch(r"test_\w+(?:\.\w+){2,}", value)
                for value in item["tests"]
            ):
                raise ValueError("场景测试必须是完整 unittest ID。")
    else:
        if asset.get("status") not in ("active", "deprecated") or asset.get(
            "outcome"
        ) not in ("observed", "verified", "reworked", "rejected"):
            raise ValueError("经验状态／实际结果无效。")
        for field in ("evidence", "recommendation"):
            text(asset.get(field), field)
        for field in ("applies_when", "counterexamples"):
            strings(asset.get(field), field)
        strings(asset.get("supersedes"), "替代关系", nonempty=False)
        for previous in asset["supersedes"]:
            identifier(previous)
            if previous == asset["id"]:
                raise ValueError("经验不能替代自身。")


def load_asset(root, kind, asset_id):
    path = asset_path(root, kind, asset_id)
    asset = json.loads(read_public(root, path.relative_to(root).as_posix()))
    validate_asset(asset, kind)
    if asset["id"] != asset_id:
        raise ValueError("资产 ID 与文件名不一致。")
    for source in asset["sources"]:
        public_path(root, source["path"])
    return asset


def freshness(root, asset):
    gaps = []
    for item in asset["sources"]:
        try:
            if content_hash(root, item["path"]) != item["sha256"]:
                gaps.append(f"来源已变化：{item['path']}")
        except (ValueError, OSError):
            gaps.append(f"来源不可用：{item['path']}")
    return gaps


def experience_graph(assets):
    graph = {item["id"]: item["supersedes"] for item in assets}
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError("经验替代关系形成循环。")
        if node in visited:
            return
        if node not in graph:
            raise ValueError(f"被替代经验不存在：{node}")
        visiting.add(node)
        for previous in graph[node]:
            visit(previous)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def put(root, kind, file):
    """Import a public JSON payload. Revisions, hashes and history are assigned here."""
    from workflow import save_json, now

    payload = json.loads(read_public(root, file))
    if not isinstance(payload, dict):
        raise ValueError("资产输入必须是对象。")
    path = asset_path(root, kind, payload.get("id"))
    if public_path(root, file) == path:
        raise ValueError("输入使用独立文件，避免编辑覆盖原版本；由 put 写入正式资产。")
    previous = load_asset(root, kind, payload["id"]) if path.exists() else None
    names = payload.get("sources")
    strings(names, "公开来源文件")
    if any(public_path(root, name) == path for name in names):
        raise ValueError("资产不能以自身作为证据来源。")
    payload.update(
        version=1,
        kind=kind,
        revision=previous["revision"] + 1 if previous else 1,
        sources=[dict(path=name, sha256=content_hash(root, name)) for name in names],
        history=copy.deepcopy(previous["history"]) if previous else [],
        updated_at=now(),
    )
    if previous:
        payload["history"].append(
            dict(
                source=payload.get("source"),
                at=now(),
                previous={
                    name: value for name, value in previous.items() if name != "history"
                },
            )
        )
    validate_asset(payload, kind)
    if kind == "experience":
        assets = [
            load_asset(root, kind, item.stem)
            for item in (root / DIRECTORIES[kind]).glob("*.json")
            if item != path
        ]
        experience_graph([*assets, payload])
    save_json(path, payload)
    return dict(
        id=payload["id"],
        kind=kind,
        revision=payload["revision"],
        digest=digest(payload),
        path=path.relative_to(root).as_posix(),
    )


def context_map(root):
    path = root / "docs/context-map.json"
    if not path.exists():
        return []  # Legacy and minimal repositories remain usable.
    value = json.loads(read_public(root, "docs/context-map.json"))
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("modules"), list)
    ):
        raise ValueError("上下文地图结构无效。")
    ids = set()
    for module in value["modules"]:
        if not isinstance(module, dict):
            raise ValueError("模块条目无效。")
        identifier(module.get("id"))
        if module["id"] in ids:
            raise ValueError("上下文模块 ID 重复。")
        ids.add(module["id"])
        text(module.get("title"), "模块标题")
        strings(module.get("tags"), "模块检索标签")
        strings(module.get("paths"), "模块路径")
        for name in module["paths"]:
            public_path(root, name)
    return value["modules"]


def catalog(root):
    result = []
    for module in context_map(root):
        gaps, hashes = [], {}
        for name in module["paths"]:
            try:
                hashes[name] = content_hash(root, name)
            except (ValueError, OSError):
                gaps.append(f"来源不可用：{name}")
        result.append(
            dict(
                kind="module",
                id=module["id"],
                title=module["title"],
                tags=module["tags"],
                paths=module["paths"],
                hashes=hashes,
                status="current",
                gaps=gaps,
            )
        )
    experiences = []
    for kind, directory in DIRECTORIES.items():
        for path in sorted((root / directory).glob("*.json")):
            asset = load_asset(root, kind, path.stem)
            entry = dict(
                kind=kind,
                id=asset["id"],
                title=asset["title"],
                tags=asset["tags"],
                path=path.relative_to(root).as_posix(),
                revision=asset["revision"],
                digest=digest(asset),
                status=asset["status"],
                gaps=freshness(root, asset),
            )
            if kind == "story":
                entry["scenarios"] = [
                    dict(id=item["id"], title=item["title"])
                    for item in asset["scenarios"]
                ]
            else:
                experiences.append(asset)
                entry.update(
                    outcome=asset["outcome"],
                    applies_when=asset["applies_when"],
                    supersedes=asset["supersedes"],
                )
            result.append(entry)
    experience_graph(experiences)
    replaced = {}
    for asset in experiences:
        # Supersession remains visible even if the replacement later becomes stale.
        for old in asset["supersedes"]:
            replaced.setdefault(old, []).append(asset["id"])
    for entry in result:
        if entry["kind"] == "experience":
            entry["superseded_by"] = replaced.get(entry["id"], [])
    return result


def doctor(root):
    errors = []
    try:
        for entry in catalog(root):
            # Source drift is reported by retrieval and linked task gates, not a
            # repository-wide failure that blocks unrelated work.
            if entry["kind"] == "module":
                errors.extend(entry["gaps"])
    except (ValueError, OSError, KeyError, TypeError) as error:
        errors.append(f"知识／上下文资产无效：{error}")
    return errors


def index(root):
    from workflow import save_json, now

    entries = catalog(root)
    value = dict(version=1, at=now(), entries=entries)
    # Never trust cached content for routing or validation; it is a derived view.
    path = root / ".workflow/context-index.json"
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("索引路径不能离开项目。")
    save_json(path, value)
    return value


def search_score(query, entry):
    haystack = json.dumps(
        {
            name: value
            for name, value in entry.items()
            if name in ("id", "title", "tags", "scenarios", "applies_when", "paths")
        },
        ensure_ascii=False,
    ).lower()
    tokens = re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", query.lower())
    terms = set()
    for token in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            terms.update(token[i : i + 2] for i in range(len(token) - 1))
        else:
            terms.add(token)
    return sum(1 for term in terms if term in haystack)


def context(
    root,
    key=None,
    query="",
    level="overview",
    limit=8,
    max_chars=16000,
    include_inactive=False,
):
    from workflow_protocol import summary

    if (
        level not in ("overview", "related", "detail")
        or not 1 <= limit <= 30
        or not 200 <= max_chars <= 100000
    ):
        raise ValueError("上下文层级／数量／字符预算无效。")
    task = summary(root, key) if key else None
    entries = catalog(root)
    linked = {
        item["story"]
        for item in ((task or {}).get("protocol") or {}).get("stories", [])
    }
    effective_query = query or (
        (task or {}).get("request", "") if level != "overview" else ""
    )
    selected, omitted = [], []
    for entry in entries:
        relevant = entry["kind"] == "story" and entry["id"] in linked
        score = search_score(effective_query, entry) if effective_query else 1
        if not relevant and not score:
            continue
        inactive = (
            entry["status"] in ("draft", "deprecated")
            or bool(entry["gaps"])
            or bool(entry.get("superseded_by"))
            or entry.get("outcome") in ("rejected", "reworked")
        )
        if inactive and not include_inactive and not relevant:
            omitted.append(
                dict(
                    kind=entry["kind"],
                    id=entry["id"],
                    reason="草案、来源过期、已废弃、被替代或结果不适用",
                    gaps=entry["gaps"],
                )
            )
            continue
        selected.append(
            {
                **entry,
                "score": score,
                "reason": "当前 AC 引用"
                if relevant
                else "检索标签／标题／适用条件命中"
                if effective_query
                else "目录概览",
                "linked": relevant,
            }
        )
    selected.sort(
        key=lambda item: (not item["linked"], -item["score"], item["kind"], item["id"])
    )
    kept = [item for i, item in enumerate(selected) if i < limit or item["linked"]]
    omitted.extend(
        dict(kind=item["kind"], id=item["id"], reason="数量上限，缩小查询继续加载")
        for item in selected
        if item not in kept
    )
    output = dict(
        level=level,
        task=task,
        required_context=["AGENTS.md", "docs/DEVELOPMENT.md", "docs/ARCHITECTURE.md"],
        entries=kept,
        omitted=omitted,
        budget=dict(
            max_chars=max_chars,
            used_chars=0,
            note="预算仅限制展开正文；任务授权、缺口及关联契约元数据不会被截断",
        ),
    )
    if level == "detail" and not (effective_query or linked):
        raise ValueError("detail 需要查询或关联任务，不能全量展开。")
    remaining = max_chars
    if level in ("related", "detail"):
        for item in kept:
            if item["kind"] != "module":
                asset = load_asset(root, item["kind"], item["id"])
                current = {
                    name: value for name, value in asset.items() if name != "history"
                }
                body = json.dumps(current, ensure_ascii=False, indent=2)
                if len(body) <= remaining:
                    item["content"] = current
                    remaining -= len(body)
                else:
                    item["deferred"] = (
                        "正文超出预算；按 ID 单独查询或增加预算，不截断契约"
                    )
            elif level == "detail":
                item["files"] = []
                for name in item["paths"]:
                    try:
                        body = read_public(root, name)
                    except (ValueError, OSError):
                        item["files"].append(dict(path=name, deferred="来源不可用"))
                        continue
                    if len(body) <= remaining:
                        item["files"].append(dict(path=name, content=body))
                        remaining -= len(body)
                    else:
                        item["files"].append(
                            dict(path=name, deferred="正文超出预算；直接按模块路径读取")
                        )
    output["budget"]["used_chars"] = max_chars - remaining
    return output


def asset_command(root, action, kind, file=None, asset_id=None, full=False):
    if action == "put":
        if not file or asset_id or full:
            raise ValueError("asset put 使用 --file 独立输入文件。")
        return put(root, kind, file)
    if not asset_id or file:
        raise ValueError("asset show 使用 --id。")
    asset = load_asset(root, kind, asset_id)
    return dict(
        asset=asset
        if full
        else {name: value for name, value in asset.items() if name != "history"},
        freshness_gaps=freshness(root, asset),
    )


def register(commands):
    parser = commands.add_parser("asset", help="管理版本化故事线和经验")
    parser.add_argument("action", choices=("put", "show"))
    parser.add_argument("kind", choices=tuple(DIRECTORIES))
    parser.add_argument("--file")
    parser.add_argument("--id", dest="asset_id")
    parser.add_argument("--full", action="store_true")
    commands.add_parser("index", help="重建可丢弃的上下文索引")
    parser = commands.add_parser(
        "context", help="概览 → 相关契约／经验 → 按预算展开源码"
    )
    parser.add_argument("key", nargs="?")
    parser.add_argument("--query", default="")
    parser.add_argument(
        "--level", choices=("overview", "related", "detail"), default="overview"
    )
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=16000)
    parser.add_argument("--include-inactive", action="store_true")
    return dict(asset=asset_command, index=index, context=context)
