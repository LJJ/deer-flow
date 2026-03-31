"""LangGraph Server 入口 — 注册 filming sub-agent 后返回 lead agent factory"""

from deerflow.subagents.registry import register_subagents, get_subagent_names
from .subagents import FILMING_SUBAGENTS

# 注册 filming sub-agent（server 启动时执行一次）
if "scene-curator" not in get_subagent_names():
    register_subagents(FILMING_SUBAGENTS)

# 导出 make_lead_agent 供 langgraph.json 使用
from deerflow.agents import make_lead_agent  # noqa: E402, F401
