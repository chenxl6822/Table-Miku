from __future__ import annotations

import re
from dataclasses import dataclass

from .agent_models import CoachResponse, ReadResource


RESOURCE_LABELS = {
    ReadResource.KNOWLEDGE: "知识库",
    ReadResource.REVIEW: "复习与错题",
    ReadResource.GOALS: "学习目标",
    ReadResource.TIMETABLE: "课程表",
    ReadResource.INTERVIEWS: "投递/面试记录",
}


@dataclass(frozen=True)
class ResourceAccessRule:
    resource: ReadResource
    patterns: tuple[re.Pattern[str], ...]


RESOURCE_ACCESS_RULES = (
    ResourceAccessRule(
        ReadResource.KNOWLEDGE,
        (
            re.compile(r"我的(?:本地)?知识库"),
            re.compile(r"(?:根据|基于|按照|结合|参考|检索|搜索|查询|从).{0,8}(?:本地)?知识库"),
            re.compile(r"本地知识(?:卡|来源|内容)"),
            re.compile(r"\b(?:my|local)knowledgebase\b", re.IGNORECASE),
        ),
    ),
    ResourceAccessRule(
        ReadResource.REVIEW,
        (
            re.compile(r"我的(?:今日复习|复习记录|错题|历史作答)"),
            re.compile(r"今日复习|待复习|错题本|我的错题|历史作答|复习记录"),
            re.compile(r"上次(?:回答|作答)"),
            re.compile(r"\bmy(?:reviews?|mistakes?|answerhistory)\b", re.IGNORECASE),
        ),
    ),
    ResourceAccessRule(
        ReadResource.GOALS,
        (
            re.compile(r"我的学习目标"),
            re.compile(r"(?:根据|基于|按照|结合|参考).{0,8}学习目标"),
            re.compile(r"学习目标.{0,8}(?:制定|安排|规划|生成)"),
            re.compile(r"\bmy(?:learning)?goals?\b", re.IGNORECASE),
        ),
    ),
    ResourceAccessRule(
        ReadResource.TIMETABLE,
        (
            re.compile(r"我的(?:课程表|课表|课程安排)"),
            re.compile(r"(?:根据|基于|按照|结合|参考|查看|读取).{0,8}(?:课程表|课表)"),
            re.compile(r"(?:今天|明天|本周|下周).{0,6}(?:有什么课|上什么课|课程安排)"),
            re.compile(r"下一节课|课程时间表"),
            re.compile(r"\bmy(?:timetable|schedule|classes)\b", re.IGNORECASE),
        ),
    ),
    ResourceAccessRule(
        ReadResource.INTERVIEWS,
        (
            re.compile(r"我的(?:投递(?:记录|情况|进度|状态)|面试(?:复盘|记录|安排|进度)|求职记录|申请(?:记录|进度|状态))"),
            re.compile(r"投递记录|面试复盘|求职记录|申请记录"),
            re.compile(r"(?:根据|基于|按照|结合|参考|查看|读取).{0,8}(?:投递|面试复盘|求职记录|申请记录)"),
            re.compile(r"\bmy(?:applications?|interviews?|jobrecords?)\b", re.IGNORECASE),
        ),
    ),
)


def required_ungranted_resource(message: str, grants: dict[str, bool]) -> ReadResource | None:
    compact = re.sub(r"\s+", "", message)
    for rule in RESOURCE_ACCESS_RULES:
        if grants.get(rule.resource.value, False):
            continue
        if any(pattern.search(compact) for pattern in rule.patterns):
            return rule.resource
    return None


def blocked_resource_response(message: str, grants: dict[str, bool]) -> CoachResponse | None:
    resource = required_ungranted_resource(message, grants)
    if resource is None:
        return None
    label = RESOURCE_LABELS[resource]
    return CoachResponse(
        body=(
            f"“{label}”目前未授权，因此我不能读取或声称依据其中的个人数据回答。\n\n"
            f"请在 Agent 中心右侧勾选“{label}（只读）”后重新发送；"
            "你也可以保持关闭，并让我给出一份不使用个人数据的通用回答。"
        ),
        intent="permission_required",
        suggested_actions=[f"开启{label}只读授权", "改为通用请求"],
    )


def format_grant_summary(grants: dict[str, bool]) -> str:
    states = []
    for resource, label in RESOURCE_LABELS.items():
        states.append(f"{label}={'允许只读' if grants.get(resource.value, False) else '未授权'}")
    return "；".join(states)
