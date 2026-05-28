from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .storage import load_goals, save_goals


INTERNSHIP_PLAN = [
    {
        "phase": "基础补强",
        "title": "语言基础与环境整理",
        "task": "选择主攻语言 Python/Java/C++ 之一，复习变量、函数、集合、异常、文件 IO，并整理 10 条易错点。",
        "output": "完成一页语法速查表，并写 3 个小练习。",
    },
    {
        "phase": "算法入门",
        "title": "数组与字符串",
        "task": "刷 2 道数组/字符串题，重点练双指针、哈希计数、边界条件。",
        "output": "每题写清思路、复杂度、错因。",
    },
    {
        "phase": "数据结构",
        "title": "链表、栈、队列",
        "task": "复习链表增删查、栈队列应用，手写一个最小队列或括号匹配 demo。",
        "output": "提交 demo，并记录 API 设计。",
    },
    {
        "phase": "项目实践",
        "title": "项目选题与 README",
        "task": "确定一个可展示项目：桌宠、博客、待办、数据看板或后端 API，补齐 README 的功能、截图、运行步骤。",
        "output": "README 能让陌生人 5 分钟跑起来。",
    },
    {
        "phase": "数据库",
        "title": "SQL 与索引",
        "task": "练习 SELECT/JOIN/GROUP BY，理解索引、事务、唯一约束，完成 5 个 SQL 练习。",
        "output": "整理一份 SQL 错题笔记。",
    },
    {
        "phase": "工程能力",
        "title": "Git 与代码质量",
        "task": "练习分支、commit、pull request 流程，为项目加入格式化、基础测试或静态检查。",
        "output": "项目至少有 1 个可运行测试或检查命令。",
    },
    {
        "phase": "简历准备",
        "title": "简历第一版",
        "task": "写一页简历，突出课程项目、技术栈、职责、结果，用数字描述成果。",
        "output": "完成 PDF 简历 v1，并列出 3 个需要补强的经历。",
    },
    {
        "phase": "面试表达",
        "title": "项目讲解",
        "task": "用 STAR 法讲一个项目：背景、任务、行动、结果，并准备 5 个追问答案。",
        "output": "录音或写稿复盘 10 分钟。",
    },
    {
        "phase": "投递准备",
        "title": "岗位画像",
        "task": "找 5 个实习 JD，提取高频技能词，标出自己已经具备/需要补的能力。",
        "output": "形成一张技能差距表。",
    },
    {
        "phase": "冲刺复盘",
        "title": "周复盘与下周计划",
        "task": "复盘本周任务完成度、卡点、项目进展和算法题错因，安排下周 3 个优先事项。",
        "output": "写一段 150 字复盘。",
    },
]

GENERAL_PLAN = [
    {
        "phase": "目标拆解",
        "title": "明确可交付结果",
        "task": "把目标拆成 3 个可交付结果，选出今天能完成的最小一步。",
        "output": "写下今天的完成标准。",
    },
    {
        "phase": "专注执行",
        "title": "45 分钟深度学习",
        "task": "关闭干扰，专注学习 45 分钟，中途只记录问题不跳走。",
        "output": "记录 3 条收获和 1 个疑问。",
    },
    {
        "phase": "练习验证",
        "title": "做一个小练习",
        "task": "把刚学的知识做成一个可运行练习，确认不是只看懂。",
        "output": "保存代码、截图或笔记。",
    },
    {
        "phase": "复盘整理",
        "title": "整理下一步",
        "task": "复盘今天卡点，整理资料入口，写明明天第一步。",
        "output": "完成 5 分钟复盘。",
    },
]


def _template_for(goal: dict[str, Any]) -> list[dict[str, str]]:
    text = f"{goal.get('title', '')} {goal.get('description', '')}"
    keywords = ["实习", "公司", "面试", "简历", "算法", "开发", "后端", "前端", "软件"]
    return INTERNSHIP_PLAN if any(keyword in text for keyword in keywords) else GENERAL_PLAN


def build_plan(goal: dict[str, Any], days: int = 60) -> list[dict[str, str]]:
    template = _template_for(goal)
    plan: list[dict[str, str]] = []
    for index in range(days):
        item = template[index % len(template)]
        week = index // 7 + 1
        plan.append(
            {
                "day": str(index + 1),
                "week": str(week),
                "phase": item["phase"],
                "title": f"第 {index + 1} 天：{item['title']}",
                "task": item["task"],
                "output": item["output"],
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
        plan = goal.get("plan") or []
        if len(plan) < 30 or "output" not in plan[0]:
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
        tasks.append(
            f"{goal.get('title', '学习目标')}\n"
            f"阶段：{current.get('phase', '执行')}\n"
            f"任务：{current['task']}\n"
            f"交付：{current.get('output', '完成并记录结果')}\n"
            f"建议时长：{minutes} 分钟"
        )

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
