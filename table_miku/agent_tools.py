from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from .agent_models import (
    HistoryQueryArgs,
    ReadResource,
    ResourceQueryArgs,
    ReviewQueryArgs,
    SearchKnowledgeArgs,
    SourceReference,
    ApplyLearningPlanArgs,
    ApprovalRequest,
    MarkLearnedArgs,
    RecordAnswerArgs,
    SyncKnowledgeArgs,
    WRITE_ARGUMENT_MODELS,
)
from .agent_store import AgentStore, redact_text
from .assistant_data import (
    load_application_records,
    load_interview_reviews,
    load_timetable,
)
from .knowledge_service import (
    due_question_items,
    answer_key_point_hints,
    mark_knowledge_card_learned,
    mistake_question_items,
    question_attempt_items,
    record_question_answer,
    search_knowledge_cards,
    sync_local_knowledge,
)
from .storage import load_goals, save_goals


MAX_TOOL_ITEMS = 8
MAX_TOOL_CHARS = 12_000


@dataclass
class AgentRunContext:
    store: AgentStore
    session_id: str
    repair_attempts: int = 0
    sources: dict[str, SourceReference] = field(default_factory=dict)
    authorized_at: dict[str, str] = field(default_factory=dict)

    def grant(self, resource: ReadResource) -> bool:
        return self.store.resource_grants().get(resource.value, False)


def _permission_error(resource: ReadResource) -> str:
    return json.dumps(
        {"error": "resource_not_authorized", "resource": resource.value},
        ensure_ascii=False,
    )


def _bounded_json(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= MAX_TOOL_CHARS:
        return text
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = list(payload["items"])
        while items and len(json.dumps({**payload, "items": items}, ensure_ascii=False)) > MAX_TOOL_CHARS:
            items.pop()
        payload = {**payload, "items": items, "truncated": True}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:MAX_TOOL_CHARS]
    return text[:MAX_TOOL_CHARS]


def _source_from_card(card: dict[str, Any]) -> list[SourceReference]:
    result: list[SourceReference] = []
    for source in (card.get("sources") or [])[:3]:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or source.get("url") or source.get("name") or "").strip()
        if not source_id:
            continue
        result.append(
            SourceReference(
                source_id=source_id,
                title=str(source.get("name") or card.get("title") or "本地知识来源"),
                location=str(source.get("url") or ""),
                excerpt=str(card.get("overview") or "")[:300],
            )
        )
    return result


async def _invoke_validated(
    context: AgentRunContext,
    raw_arguments: str,
    model: type[BaseModel],
    callback: Callable[[BaseModel], str],
) -> str:
    try:
        parsed = model.model_validate_json(raw_arguments)
    except ValidationError as exc:
        if context.repair_attempts < 1:
            context.repair_attempts += 1
            return _bounded_json(
                {
                    "error": "invalid_tool_arguments",
                    "repair_allowed": True,
                    "details": exc.errors(include_url=False),
                }
            )
        return _bounded_json(
            {
                "error": "invalid_tool_arguments",
                "repair_allowed": False,
                "message": "工具参数再次校验失败，本次调用已终止。",
            }
        )
    return callback(parsed)


def create_read_tools() -> list[Any]:
    from agents import FunctionTool

    def make_tool(
        name: str,
        description: str,
        model: type[BaseModel],
        callback: Callable[[AgentRunContext, BaseModel], str],
    ) -> Any:
        async def invoke(tool_context: Any, raw_arguments: str) -> str:
            context: AgentRunContext = tool_context.context
            return await _invoke_validated(
                context,
                raw_arguments,
                model,
                lambda parsed: callback(context, parsed),
            )

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=model.model_json_schema(),
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    return [
        make_tool(
            "search_local_knowledge",
            "Search indexed local knowledge cards and source-backed answers. Never reads raw Vault files.",
            SearchKnowledgeArgs,
            _search_knowledge,
        ),
        make_tool(
            "list_due_review_questions",
            "List due review questions without revealing reference answers.",
            ReviewQueryArgs,
            _due_reviews,
        ),
        make_tool(
            "list_mistake_questions",
            "List questions in the local mistake book without reference answers.",
            ReviewQueryArgs,
            _mistakes,
        ),
        make_tool(
            "list_answer_history",
            "Read recent attempts for one question.",
            HistoryQueryArgs,
            _answer_history,
        ),
        make_tool("list_learning_goals", "Read local learning goals.", ResourceQueryArgs, _goals),
        make_tool("list_timetable", "Read the local course timetable.", ResourceQueryArgs, _timetable),
        make_tool(
            "list_interview_context",
            "Read redacted application and interview review summaries.",
            ResourceQueryArgs,
            _interviews,
        ),
    ]


def create_write_tools() -> list[Any]:
    from agents import FunctionTool

    tools = []
    descriptions = {
        "mark_knowledge_learned": "Mark one local knowledge card as learned. Always needs per-call approval.",
        "record_review_answer": "Persist one user answer and self-rating. Never decides mastery automatically.",
        "apply_learning_plan": "Apply a proposed local learning plan after explicit approval.",
        "sync_local_knowledge": "Run configured read-only Vault indexing after explicit approval.",
    }
    for name, model in WRITE_ARGUMENT_MODELS.items():
        async def invoke(tool_context: Any, raw_arguments: str, name=name, model=model) -> str:
            context: AgentRunContext = tool_context.context
            return await _invoke_validated(
                context,
                raw_arguments,
                model,
                lambda parsed: _execute_write(context, name, parsed),
            )

        tools.append(
            FunctionTool(
                name=name,
                description=descriptions[name],
                params_json_schema=model.model_json_schema(),
                on_invoke_tool=invoke,
                strict_json_schema=False,
                needs_approval=True,
            )
        )
    return tools


def approval_preview(tool_name: str, raw_arguments: str) -> ApprovalRequest:
    model = WRITE_ARGUMENT_MODELS.get(tool_name)
    if model is None:
        raise ValueError(f"不允许审批未知工具：{tool_name}")
    parsed = model.model_validate_json(raw_arguments)
    values = parsed.model_dump()
    operation_id = str(values.pop("operation_id"))
    if tool_name == "mark_knowledge_learned":
        return ApprovalRequest(
            operation_id=operation_id,
            tool_name=tool_name,
            title="标记知识卡已学",
            target=str(values["card_id"]),
            fields=values,
            reversible=False,
            approve_label="标记这张知识卡已学",
        )
    if tool_name == "record_review_answer":
        return ApprovalRequest(
            operation_id=operation_id,
            tool_name=tool_name,
            title="记录本次作答",
            target=str(values["question_id"]),
            fields=values,
            reversible=False,
            approve_label="记录本次作答",
        )
    if tool_name == "apply_learning_plan":
        return ApprovalRequest(
            operation_id=operation_id,
            tool_name=tool_name,
            title="应用学习计划",
            target=str(values["goal_title"]),
            fields=values,
            reversible=False,
            approve_label="应用学习计划",
        )
    return ApprovalRequest(
        operation_id=operation_id,
        tool_name=tool_name,
        title="同步本地知识库",
        target="已配置的 Obsidian 白名单目录",
        fields=values,
        reversible=False,
        approve_label="开始只读同步",
    )


def _execute_write(context: AgentRunContext, tool_name: str, raw: BaseModel) -> str:
    operation_id = str(getattr(raw, "operation_id"))
    existing = context.store.get_receipt(operation_id)
    if existing is not None:
        return _bounded_json({"idempotent": True, "receipt": existing})
    preview = approval_preview(tool_name, raw.model_dump_json()).model_dump()
    authorized_at = context.authorized_at.get(operation_id)
    if not authorized_at:
        return _bounded_json({"error": "approval_missing", "operation_id": operation_id})

    if tool_name == "mark_knowledge_learned":
        args = MarkLearnedArgs.model_validate(raw)
        result = {"questions_scheduled": mark_knowledge_card_learned(args.card_id)}
    elif tool_name == "record_review_answer":
        args = RecordAnswerArgs.model_validate(raw)
        question = next(
            (item for item in due_question_items(limit=100) + mistake_question_items(limit=100) if item.get("id") == args.question_id),
            {"key_points": args.matched_points},
        )
        matched = answer_key_point_hints(question, args.user_answer)
        result = record_question_answer(
            args.question_id,
            args.self_rating,
            args.user_answer,
            matched_points=matched,
        )
    elif tool_name == "apply_learning_plan":
        args = ApplyLearningPlanArgs.model_validate(raw)
        goals = load_goals()
        goal = next((item for item in goals if str(item.get("title")) == args.goal_title), None)
        if goal is None:
            goal = {"title": args.goal_title}
            goals.append(goal)
        goal["daily_minutes"] = args.daily_minutes
        goal["plan"] = args.tasks
        save_goals(goals)
        result = {"goal_title": args.goal_title, "task_count": len(args.tasks)}
    else:
        SyncKnowledgeArgs.model_validate(raw)
        summary = sync_local_knowledge()
        result = {
            key: summary.get(key)
            for key in ("available", "scanned", "created", "updated", "deleted", "questions", "errors")
        }
    receipt = context.store.save_receipt(
        operation_id=operation_id,
        session_id=context.session_id,
        tool_name=tool_name,
        preview=preview,
        result=result,
        authorized_at=authorized_at,
        status="completed",
        reversible=False,
    )
    return _bounded_json({"receipt": receipt})


def _search_knowledge(context: AgentRunContext, raw: BaseModel) -> str:
    args = SearchKnowledgeArgs.model_validate(raw)
    if not context.grant(ReadResource.KNOWLEDGE):
        return _permission_error(ReadResource.KNOWLEDGE)
    cards = search_knowledge_cards(args.query, limit=min(args.limit, MAX_TOOL_ITEMS))
    items: list[dict[str, Any]] = []
    for card in cards[:MAX_TOOL_ITEMS]:
        sources = _source_from_card(card)
        for source in sources:
            context.sources[source.source_id] = source
        qas = []
        for pair in (card.get("qa_pairs") or [])[:4]:
            question = str(pair.get("question") or "").strip()
            answer = str(pair.get("answer_detail") or pair.get("answer") or "").strip()
            if question and answer:
                qas.append({"question": question[:300], "answer": answer[:1200]})
        items.append(
            {
                "card_id": str(card.get("id") or ""),
                "title": str(card.get("title") or "")[:240],
                "topic": str(card.get("topic") or "")[:120],
                "overview": str(card.get("overview") or "")[:1400],
                "qa": qas,
                "source_ids": [source.source_id for source in sources],
            }
        )
    return _bounded_json({"items": items, "count": len(items)})


def _question_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": str(item.get("id") or ""),
        "card_id": str(item.get("card_id") or ""),
        "topic": str(item.get("topic") or "")[:120],
        "question": str(item.get("question") or "")[:1000],
        "difficulty": str(item.get("difficulty") or "normal"),
        "next_review_at": str(item.get("next_review_at") or ""),
        "wrong_count": int(item.get("wrong_count") or 0),
    }


def _due_reviews(context: AgentRunContext, raw: BaseModel) -> str:
    args = ReviewQueryArgs.model_validate(raw)
    if not context.grant(ReadResource.REVIEW):
        return _permission_error(ReadResource.REVIEW)
    items = [_question_view(item) for item in due_question_items(limit=args.limit)[:MAX_TOOL_ITEMS]]
    return _bounded_json({"items": items, "answers_hidden": True})


def _mistakes(context: AgentRunContext, raw: BaseModel) -> str:
    args = ReviewQueryArgs.model_validate(raw)
    if not context.grant(ReadResource.REVIEW):
        return _permission_error(ReadResource.REVIEW)
    items = [_question_view(item) for item in mistake_question_items(limit=args.limit)[:MAX_TOOL_ITEMS]]
    return _bounded_json({"items": items, "answers_hidden": True})


def _answer_history(context: AgentRunContext, raw: BaseModel) -> str:
    args = HistoryQueryArgs.model_validate(raw)
    if not context.grant(ReadResource.REVIEW):
        return _permission_error(ReadResource.REVIEW)
    attempts = question_attempt_items(args.question_id, limit=args.limit)
    items = [
        {
            "answered_at": item.get("answered_at"),
            "user_answer": redact_text(str(item.get("user_answer") or ""), 1200),
            "self_rating": item.get("result"),
            "matched_points": item.get("matched_points") or [],
        }
        for item in attempts[:MAX_TOOL_ITEMS]
    ]
    return _bounded_json({"items": items})


def _goals(context: AgentRunContext, raw: BaseModel) -> str:
    args = ResourceQueryArgs.model_validate(raw)
    if not context.grant(ReadResource.GOALS):
        return _permission_error(ReadResource.GOALS)
    items = []
    for goal in load_goals()[: args.limit]:
        items.append(
            {
                "title": str(goal.get("title") or "")[:200],
                "description": str(goal.get("description") or "")[:800],
                "daily_minutes": goal.get("daily_minutes"),
                "target_date": goal.get("target_date"),
                "plan": [str(item)[:300] for item in (goal.get("plan") or [])[:8]],
            }
        )
    return _bounded_json({"items": items})


def _timetable(context: AgentRunContext, raw: BaseModel) -> str:
    args = ResourceQueryArgs.model_validate(raw)
    if not context.grant(ReadResource.TIMETABLE):
        return _permission_error(ReadResource.TIMETABLE)
    allowed = {"weekday", "start", "end", "section", "course"}
    items = [{key: row.get(key) for key in allowed if row.get(key)} for row in load_timetable()[: args.limit]]
    return _bounded_json({"items": items})


def _interviews(context: AgentRunContext, raw: BaseModel) -> str:
    args = ResourceQueryArgs.model_validate(raw)
    if not context.grant(ReadResource.INTERVIEWS):
        return _permission_error(ReadResource.INTERVIEWS)
    reviews = [
        {
            "company": str(item.get("company") or "")[:100],
            "round": str(item.get("round") or "")[:80],
            "summary": redact_text(str(item.get("summary") or ""), 900),
            "next_step": redact_text(str(item.get("next_step") or ""), 300),
        }
        for item in load_interview_reviews()[-args.limit :]
    ]
    applications = [
        {
            "company": str(item.get("company") or "")[:100],
            "position": str(item.get("position") or "")[:100],
            "status": str(item.get("status") or "")[:80],
            "next_step": redact_text(str(item.get("next_step") or ""), 300),
        }
        for item in load_application_records()[-args.limit :]
    ]
    return _bounded_json({"interviews": reviews, "applications": applications})
