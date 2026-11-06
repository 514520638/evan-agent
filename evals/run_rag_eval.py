"""用真实知识库、Embedding 和生成模型重复评估访客问答。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from evan_agent.auth import AuthContext
from evan_agent.knowledge import KnowledgeStore
from evan_agent.retrieval import Retriever
from evan_agent.service import build_agent
from evan_agent.sessions import SessionStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "rag_cases.json"
DEFAULT_OUTPUT = ROOT / ".local" / "evaluation"
CITATION = re.compile(r"\[资料:([^\]]+)\]")


def knowledge_digest(store: KnowledgeStore) -> str:
    """对资料表做稳定摘要，用于证明访客评估没有改写知识库。"""
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT * FROM profile_entries ORDER BY id").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def matches(text: str, pattern: str) -> bool:
    """评估规则使用正则；普通中英文片段也可直接匹配。"""
    return re.search(pattern, text, re.IGNORECASE) is not None


def score_answer(case: dict, answer: str, public_ids: set[str], unchanged: bool) -> dict:
    """评分核心事实、禁用推断和引用；不调用另一个模型充当裁判。"""
    missing = [pattern for pattern in case.get("required", []) if not matches(answer, pattern)]
    missing_groups = [group for group in case.get("required_any", [])
                      if not any(matches(answer, pattern) for pattern in group)]
    forbidden = [pattern for pattern in case.get("forbidden", []) if matches(answer, pattern)]
    citations = CITATION.findall(answer)
    invalid_citations = [item for item in citations if item not in public_ids]
    expected = set(case.get("expected_sources", []))
    expected_citation = not expected or bool(expected.intersection(citations))
    write_safe = not case.get("require_knowledge_unchanged") or unchanged
    passed = not missing and not missing_groups and not forbidden and not invalid_citations
    passed = passed and expected_citation and write_safe
    return {
        "passed": passed,
        "missing": missing,
        "missing_groups": missing_groups,
        "forbidden_hits": forbidden,
        "citations": citations,
        "invalid_citations": invalid_citations,
        "expected_citation": expected_citation,
        "write_safe": write_safe,
    }


def score_retrieval(case: dict, top3: list[dict], meta: dict) -> dict:
    """检查相关资料是否进入前三，并记录本次是否真的用了向量检索。"""
    expected = set(case.get("expected_sources", []))
    hits = expected.intersection(item["id"] for item in top3)
    minimum = case.get("retrieval_min_hits", 0)
    return {
        "passed": len(hits) >= minimum,
        "hit_count": len(hits),
        "required_hits": minimum,
        "hit_ids": sorted(hits),
        "mode": meta.get("mode"),
        "hybrid": meta.get("mode") == "hybrid",
    }


def run_case(case: dict, store: KnowledgeStore, retriever: Retriever, agent,
             sessions: SessionStore, guest: AuthContext, public_ids: set[str], before: str) -> dict:
    """每个独立问题使用新会话，避免上一题把答案泄漏给下一题。"""
    started = time.monotonic()
    row = {"id": case["id"], "question": case["question"], "review_only": case.get("review_only", False)}
    try:
        hits, meta = retriever.search(guest, case["question"], limit=5)
        top3 = [{"id": entry.id, "category": entry.category,
                 "content": chunk.text if chunk else entry.content} for entry, chunk in hits[:3]]
        conversation_id = sessions.create(guest)
        sessions.invoke(agent, guest, conversation_id, case["question"])
        # 评估原始回答的证据标注，用户界面会隐藏这些内部编号。
        answer = sessions.history(guest, conversation_id)[-1].text
        unchanged = knowledge_digest(store) == before
        row.update(answer=answer, top3=top3, retrieval=score_retrieval(case, top3, meta),
                   answer_score=score_answer(case, answer, public_ids, unchanged),
                   knowledge_unchanged=unchanged)
    except Exception as error:  # 评估需要继续跑完其他题，报告只保存异常类型。
        row["error"] = type(error).__name__
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


def run_conversation(spec: dict, store: KnowledgeStore, agent, sessions: SessionStore,
                     guest: AuthContext, public_ids: set[str], before: str) -> list[dict]:
    """连续追问共用一个会话，单独检查上下文衔接和无依据推断。"""
    conversation_id = sessions.create(guest)
    rows = []
    for index, turn in enumerate(spec["turns"], 1):
        started = time.monotonic()
        row = {"id": f'{spec["id"]}_{index}', "question": turn["question"], "multi_turn": True}
        try:
            sessions.invoke(agent, guest, conversation_id, turn["question"])
            answer = sessions.history(guest, conversation_id)[-1].text
            unchanged = knowledge_digest(store) == before
            row.update(answer=answer, answer_score=score_answer(turn, answer, public_ids, unchanged),
                       knowledge_unchanged=unchanged)
        except Exception as error:
            row["error"] = type(error).__name__
        row["seconds"] = round(time.monotonic() - started, 2)
        rows.append(row)
    return rows


def summarize(runs: list[dict]) -> dict:
    """review_only 不计最终准确率，但完整保留，等待本人确认后纳入。"""
    rows = [row for run in runs for row in run["cases"] + run["conversation"]]
    scored = [row for row in rows if not row.get("review_only")]
    answered = [row for row in scored if "answer_score" in row]
    retrieval = [row for row in scored if "retrieval" in row and row["retrieval"]["required_hits"] > 0]
    passed_answers = sum(row["answer_score"]["passed"] for row in answered)
    passed_retrieval = sum(row["retrieval"]["passed"] for row in retrieval)
    return {
        "answer_passes": passed_answers,
        "answer_total": len(answered),
        "answer_accuracy": round(passed_answers / len(answered), 4) if answered else 0,
        "retrieval_passes": passed_retrieval,
        "retrieval_total": len(retrieval),
        "retrieval_top3_accuracy": round(passed_retrieval / len(retrieval), 4) if retrieval else 0,
        "errors": sum("error" in row for row in scored),
        "knowledge_unchanged": all(row.get("knowledge_unchanged", False) for row in answered),
        "median_seconds": round(statistics.median(row["seconds"] for row in rows), 2) if rows else 0,
        "review_only_count": sum(row.get("review_only", False) for row in rows),
    }


def rescore(result: dict, spec: dict) -> dict:
    """规则调整后可复用已有模型回答，避免为了评分器修正重复产生调用成本。"""
    cases = {case["id"]: case for case in spec["cases"]}
    conversation = {f'{spec["conversation"]["id"]}_{index}': turn
                    for index, turn in enumerate(spec["conversation"]["turns"], 1)}
    public_ids = {entry["id"] for entry in result.get("public_entries", [])}
    if not public_ids:
        # 旧版结果不含此字段时，引用自身只用于验证 ID 格式；运行评估时仍使用数据库可见 ID。
        public_ids = {item for run in result["runs"] for row in run["cases"] + run["conversation"]
                      for item in CITATION.findall(row.get("answer", ""))}
    for run in result["runs"]:
        for row in run["cases"]:
            case = cases[row["id"]]
            if "answer" in row:
                row["answer_score"] = score_answer(case, row["answer"], public_ids,
                                                    row.get("knowledge_unchanged", False))
        for row in run["conversation"]:
            case = conversation[row["id"]]
            if "answer" in row:
                row["answer_score"] = score_answer(case, row["answer"], public_ids,
                                                    row.get("knowledge_unchanged", False))
    result["summary"] = summarize(result["runs"])
    return result


def write_report(result: dict, output: Path) -> None:
    """生成便于人工检查的 Markdown；JSON 保留完整机器数据。"""
    summary = result["summary"]
    lines = [
        f'# RAG 真实问答评估（{result["created_at"]}）', "",
        f'- 回答准确率：{summary["answer_passes"]}/{summary["answer_total"]} '
        f'({summary["answer_accuracy"]:.1%})',
        f'- 检索 Top3 命中率：{summary["retrieval_passes"]}/{summary["retrieval_total"]} '
        f'({summary["retrieval_top3_accuracy"]:.1%})',
        f'- 调用错误：{summary["errors"]}；知识库未变化：{summary["knowledge_unchanged"]}',
        f'- 重复次数：{result["repeat"]}；单题中位耗时：{summary["median_seconds"]} 秒',
        f'- 待本人确认、不计准确率：{summary["review_only_count"]} 次', "",
        '评分由明确事实、禁用推断、引用来源和检索来源规则确定；它不会评价文风，也不能证明未覆盖问法的表现。', "",
    ]
    for run in result["runs"]:
        lines.extend([f'## 第 {run["run"]} 轮', ""])
        for row in run["cases"] + run["conversation"]:
            if row.get("review_only"):
                status = "待确认"
            elif row.get("answer_score", {}).get("passed"):
                status = "通过"
            else:
                status = "失败"
            lines.extend([f'### {status} · {row["question"]}', "", row.get("answer", f'调用错误：{row.get("error")}'), ""])
            if "answer_score" in row and not row["answer_score"]["passed"]:
                lines.extend([f'评分详情：`{json.dumps(row["answer_score"], ensure_ascii=False)}`', ""])
            if "retrieval" in row:
                lines.extend([f'检索：`{json.dumps(row["retrieval"], ensure_ascii=False)}`', ""])
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 Evan Agent 真实 RAG 问答评估")
    parser.add_argument("--repeat", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "latest.json")
    parser.add_argument("--rescore", action="store_true", help="只按最新规则重评已有结果，不调用模型")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    spec = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.rescore:
        result = rescore(json.loads(args.output.read_text(encoding="utf-8")), spec)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        write_report(result, args.output)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return
    store = KnowledgeStore()
    guest = AuthContext.guest()
    sessions = SessionStore()
    retriever = Retriever(store)
    agent = build_agent(store, sessions)
    public_ids = {entry.id for entry in store.list_entries(guest, limit=200)}
    before = knowledge_digest(store)
    runs = []
    args.output.parent.mkdir(parents=True, exist_ok=True)

    for run_number in range(1, args.repeat + 1):
        cases = []
        for index, case in enumerate(spec["cases"], 1):
            row = run_case(case, store, retriever, agent, sessions, guest, public_ids, before)
            cases.append(row)
            print(f'第 {run_number} 轮 {index}/{len(spec["cases"])}：{case["id"]}', flush=True)
        conversation = run_conversation(spec["conversation"], store, agent, sessions, guest, public_ids, before)
        runs.append({"run": run_number, "cases": cases, "conversation": conversation})
        partial = {"created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "repeat": args.repeat, "public_entries": [{"id": item} for item in sorted(public_ids)],
                   "runs": runs}
        partial["summary"] = summarize(runs)
        args.output.write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
        write_report(partial, args.output)

    print(json.dumps(summarize(runs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
