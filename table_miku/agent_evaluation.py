from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


TOPOLOGY_EVAL_REQUEST_LIMIT = 12
TOPOLOGY_EVAL_PASS_SCORE = 80


@dataclass(frozen=True)
class TopologyEvalCase:
    name: str
    label: str
    prompt: str
    specialist_name: str
    specialist_tool: str
    required_groups: tuple[tuple[str, ...], ...]


TOPOLOGY_EVAL_CASES = (
    TopologyEvalCase(
        name="knowledge_tutor",
        label="知识讲解",
        specialist_name="Knowledge Tutor",
        specialist_tool="consult_knowledge_tutor",
        prompt=(
            "这是不含用户数据的合成评测材料。仅根据下列证据回答，不要读取任何本地资源："
            "Spring IoC 由容器负责创建和装配对象，依赖注入让对象不再自行查找依赖，从而降低耦合；"
            "工程示例是通过构造器注入 OrderService；易错点是把 IoC 误解成仅使用反射。"
            "来源编号 source_id=synthetic-spring。请给出一句话结论、原理、工程示例、易错点、面试追问和来源。"
        ),
        required_groups=(
            ("依赖注入", "di"),
            ("容器",),
            ("降低耦合", "解耦"),
            ("orderservice", "构造器"),
            ("反射",),
            ("追问",),
            ("synthetic-spring",),
        ),
    ),
    TopologyEvalCase(
        name="practice_analyst",
        label="答案分析",
        specialist_name="Practice Analyst",
        specialist_tool="consult_practice_analyst",
        prompt=(
            "这是不含用户数据的合成评测材料。题目：为什么联合索引要遵守最左前缀？"
            "候选人回答：联合索引可以让查询更快。参考要点：B+Tree 按索引列顺序排序；跳过前导列通常无法连续定位；"
            "覆盖索引可避免回表。请输出命中点、遗漏点、易错点和一个追问，并明确反馈不替代用户自评。"
        ),
        required_groups=(
            ("最左前缀",),
            ("b+tree", "b＋tree", "b 加树"),
            ("前导列",),
            ("覆盖索引",),
            ("回表",),
            ("遗漏",),
            ("追问",),
            ("不替代", "不能替代"),
        ),
    ),
    TopologyEvalCase(
        name="review_planner",
        label="复习规划",
        specialist_name="Review Planner",
        specialist_tool="consult_review_planner",
        prompt=(
            "这是不含用户数据的合成评测材料。待复习任务：Redis 缓存一致性明天复习，关联题目 synthetic-redis-1；"
            "MySQL 事务隔离 3 天后复习，关联题目 synthetic-mysql-1。请按任务、原因、时间和关联题目生成清晰计划。"
        ),
        required_groups=(
            ("redis",),
            ("缓存一致性",),
            ("明天",),
            ("synthetic-redis-1",),
            ("mysql",),
            ("事务隔离",),
            ("3 天", "三天", "3天"),
            ("synthetic-mysql-1",),
            ("原因",),
        ),
    ),
)


def capability_supports_specialists(capability: dict[str, Any] | None) -> bool:
    value = capability or {}
    return bool(value.get("multi_agent_capable", value.get("multi_agent_enabled", False)))


def specialists_enabled(
    capability: dict[str, Any] | None,
    evaluation: dict[str, Any] | None,
) -> bool:
    return capability_supports_specialists(capability) and bool((evaluation or {}).get("passed"))


def score_topology_output(
    text: str,
    case: TopologyEvalCase,
    *,
    used_tools: Iterable[str] = (),
    use_specialists: bool,
) -> dict[str, Any]:
    normalized = " ".join(str(text).casefold().split())
    hits = [
        any(candidate.casefold() in normalized for candidate in alternatives)
        for alternatives in case.required_groups
    ]
    content_score = round(90 * sum(hits) / len(hits)) if hits else 0
    tool_names = sorted({str(name) for name in used_tools if name})
    routing_ok = case.specialist_tool in tool_names if use_specialists else True
    routing_score = 10 if use_specialists and routing_ok else 0
    return {
        "score": content_score + routing_score,
        "content_score": content_score,
        "routing_score": routing_score,
        "routing_ok": routing_ok,
        "covered_groups": sum(hits),
        "total_groups": len(hits),
        "used_tools": tool_names,
        "output": str(text).strip()[:2_000],
    }


def build_topology_evaluation(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cases_by_name = {case.name: case for case in TOPOLOGY_EVAL_CASES}
    results: list[dict[str, Any]] = []
    for sample in samples:
        name = str(sample.get("name") or "")
        case = cases_by_name.get(name)
        if case is None:
            raise ValueError(f"未知合成评测场景：{name}")
        single = score_topology_output(
            str(sample.get("single_output") or ""),
            case,
            use_specialists=False,
        )
        multi = score_topology_output(
            str(sample.get("multi_output") or ""),
            case,
            used_tools=sample.get("multi_tools") or (),
            use_specialists=True,
        )
        results.append(
            {
                "name": case.name,
                "label": case.label,
                "expected_specialist": case.specialist_tool,
                "single": single,
                "multi": multi,
            }
        )
    if len(results) != len(cases_by_name) or {item["name"] for item in results} != set(cases_by_name):
        raise ValueError("合成拓扑评测必须完整覆盖三个专家场景。")

    single_score = round(sum(item["single"]["score"] for item in results) / len(results))
    multi_score = round(sum(item["multi"]["score"] for item in results) / len(results))
    routing_passed = all(item["multi"]["routing_ok"] for item in results)
    passed = routing_passed and multi_score >= TOPOLOGY_EVAL_PASS_SCORE and multi_score > single_score
    if not routing_passed:
        reason = "至少一个场景没有调用对应专家。"
    elif multi_score < TOPOLOGY_EVAL_PASS_SCORE:
        reason = f"多 Agent 得分低于 {TOPOLOGY_EVAL_PASS_SCORE} 分门槛。"
    elif multi_score <= single_score:
        reason = "多 Agent 得分没有严格高于单 Agent。"
    else:
        reason = "多 Agent 达到质量门槛且严格优于单 Agent。"
    return {
        "synthetic": True,
        "real_user_data_used": False,
        "case_count": len(results),
        "request_limit": TOPOLOGY_EVAL_REQUEST_LIMIT,
        "single_score": single_score,
        "multi_score": multi_score,
        "routing_passed": routing_passed,
        "passed": passed,
        "reason": reason,
        "cases": results,
    }
