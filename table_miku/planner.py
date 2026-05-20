from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .storage import load_goals, save_goals


INTERNSHIP_TEMPLATE = [
    "复习 Python/Java/C++ 之一的语法基础，并整理 10 条易错点。",
    "刷 2 道数组或字符串算法题，记录思路和复杂度。",
    "学习数据结构：链表、栈、队列、哈希表，写一个小 demo。",
    "推进一个可展示项目：补充 README、截图和运行步骤。",
    "学习数据库基础：SQL 查询、索引、事务，完成 5 个练习。",
    "整理简历：突出课程项目、技术栈、竞赛和可量化成果。",
    "模拟面试 30 分钟：自我介绍、项目讲解、常见八股复盘。",
]

GENERAL_TEMPLATE = [
    "把目标拆成 3 个可完成的小任务，并写下今天最重要的一步。",
    "专注学习 45 分钟，结束后记录 3 条收获。",
    "做一个可验证的小练习，确保不是只看懂、而是真的会用。",
    "整理资料和笔记，把下一次学习入口放到最显眼的位置。",
    "复盘今天进度：哪里卡住了、明天先解决什么。",
]


def _template_for(goal: dict[str, Any]) -> list[str]:
    text = f"{goal.get('title', '')} {goal.get('description', '')}"
    internship_keywords = ["实习", "公司", "面试", "简历", "算法", "开发"]
    if any(keyword in text for keyword in internship_keywords):
        return INTERNSHIP_TEMPLATE
    return GENERAL_TEMPLATE


def build_plan(goal: dict[str, Any], days: int = 28) -> list[dict[str, str]]:
    template = _template_for(goal)
    plan: list[dict[str, str]] = []
    for index in range(days):
        task = template[index % len(template)]
        plan.append(
            {
                "day": str(index + 1),
                "title": f"第 {index + 1} 天学习任务",
                "task": task,
            }
        )
    return plan


def ensure_goal_plans() -> list[dict[str, Any]]:
    goals = load_goals()
    changed = False
    for goal in goals:
        if not goal.get("created_at"):
            goal["created_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
        if not goal.get("plan"):
            goal["plan"] = build_plan(goal)
            changed = True
    if changed:
        save_goals(goals)
    return goals


def today_tasks(goals: list[dict[str, Any]] | None = None) -> list[str]:
    goals = ensure_goal_plans() if goals is None else goals
    tasks: list[str] = []
    today = date.today()

    for goal in goals:
        plan = goal.get("plan") or build_plan(goal)
        created_raw = goal.get("created_at")
        try:
            created = datetime.fromisoformat(created_raw).date()
        except (TypeError, ValueError):
            created = today
        day_index = max((today - created).days, 0) % len(plan)
        current = plan[day_index]
        minutes = goal.get("daily_minutes", 60)
        tasks.append(f"{goal.get('title', '学习目标')}：{current['task']}（建议 {minutes} 分钟）")

    return tasks


def add_goal(title: str, description: str = "", daily_minutes: int = 60) -> dict[str, Any]:
    goal = {
        "title": title.strip() or "新的学习目标",
        "description": description.strip(),
        "daily_minutes": daily_minutes,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": "",
        "plan": [],
    }
    goal["plan"] = build_plan(goal)
    goals = load_goals()
    goals.append(goal)
    save_goals(goals)
    return goal
