from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ReadResource(str, Enum):
    KNOWLEDGE = "knowledge"
    REVIEW = "review"
    GOALS = "goals"
    TIMETABLE = "timetable"
    INTERVIEWS = "interviews"


class SourceReference(BaseModel):
    source_id: str
    title: str
    location: str = ""
    excerpt: str = Field(default="", max_length=600)


class ApprovalRequest(BaseModel):
    operation_id: str
    tool_name: str
    title: str
    target: str
    fields: dict[str, Any]
    reversible: bool
    approve_label: str


class CoachResponse(BaseModel):
    body: str
    intent: str = "coach"
    sources: list[SourceReference] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    approval_request: ApprovalRequest | None = None


class KnowledgeBrief(BaseModel):
    conclusion: str
    principles: list[str] = Field(default_factory=list)
    engineering_example: str = ""
    pitfalls: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)


class AnswerFeedback(BaseModel):
    covered_points: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    note: str = ""


class ReviewTask(BaseModel):
    title: str
    reason: str
    scheduled_for: str
    question_ids: list[str] = Field(default_factory=list)


class ReviewPlan(BaseModel):
    tasks: list[ReviewTask] = Field(default_factory=list)


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=6, ge=1, le=8)


class ReviewQueryArgs(BaseModel):
    limit: int = Field(default=6, ge=1, le=8)


class HistoryQueryArgs(BaseModel):
    question_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=5, ge=1, le=8)


class ResourceQueryArgs(BaseModel):
    limit: int = Field(default=5, ge=1, le=8)


class MarkLearnedArgs(BaseModel):
    operation_id: str = Field(min_length=8, max_length=120)
    card_id: str = Field(min_length=1, max_length=160)


class RecordAnswerArgs(BaseModel):
    operation_id: str = Field(min_length=8, max_length=120)
    question_id: str = Field(min_length=1, max_length=160)
    user_answer: str = Field(max_length=6000)
    self_rating: Literal["known", "fuzzy", "forgotten"]
    matched_points: list[str] = Field(default_factory=list, max_length=30)


class ApplyLearningPlanArgs(BaseModel):
    operation_id: str = Field(min_length=8, max_length=120)
    goal_title: str = Field(min_length=1, max_length=160)
    daily_minutes: int = Field(ge=5, le=720)
    tasks: list[str] = Field(min_length=1, max_length=30)

    @field_validator("tasks")
    @classmethod
    def clean_tasks(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("tasks must contain at least one non-empty task")
        return cleaned


class SyncKnowledgeArgs(BaseModel):
    operation_id: str = Field(min_length=8, max_length=120)


WRITE_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "mark_knowledge_learned": MarkLearnedArgs,
    "record_review_answer": RecordAnswerArgs,
    "apply_learning_plan": ApplyLearningPlanArgs,
    "sync_local_knowledge": SyncKnowledgeArgs,
}
