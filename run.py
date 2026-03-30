#!/usr/bin/env python3
"""生活拍摄系统入口 — 基于 DeerFlow 的角色生活拍摄

使用方式：
  # 单次拍摄（手动触发）
  python run.py once

  # 定时调度（每 N 小时自动触发）
  python run.py schedule --interval 3

  # 只运行选题（调试用）
  python run.py curate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 确保 backend 包和自定义模块可导入
sys.path.insert(0, str(Path(__file__).parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent / "src"))
os.chdir(Path(__file__).parent)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("filming")

# DeerFlow 配置路径
os.environ.setdefault("DEER_FLOW_CONFIG_PATH", str(Path(__file__).parent / "config.yaml"))
os.environ.setdefault(
    "DEER_FLOW_EXTENSIONS_CONFIG_PATH",
    str(Path(__file__).parent / "extensions_config.json"),
)

# 加载 .env（如果存在）
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

def _ensure_subagents_registered():
    """注册拍摄系统的 sub-agent 到 DeerFlow registry（延迟执行，避免循环导入）"""
    from deerflow.subagents.registry import register_subagents, get_subagent_names
    if "scene-curator" not in get_subagent_names():
        from filming_custom.subagents import FILMING_SUBAGENTS
        register_subagents(FILMING_SUBAGENTS)


FILMING_PROMPT_TEMPLATE = """你是生活拍摄系统的导演（Lead Agent）。你的职责是协调选题编辑和摄影师，完成角色日常生活的视频拍摄。

## 绝对规则
画面中只拍 AI 角色（宋玉、紫灵等），绝对不出现用户（公子），提都不要提。
角色与公子的对话，通过角色的独白、自言自语、对着镜头说话来呈现。

## 拍摄视角
有两种拍摄视角，根据指令选择：

- **"某个人的一天"**：聚焦单个角色，告诉 scene-curator 用 query_diary_raw_events 查询该角色的日记原始事件
- **"世界里的一天"**：全角色视角，告诉 scene-curator 用 query_world_events 查询世界事件

默认使用"世界里的一天"视角。

{dedup_section}

## 流程

### 第一步：选题
委派 scene-curator sub-agent，告诉它用哪个数据源和视角。

{dedup_instruction}

如果 scene-curator 返回 skip（没有值得拍摄的内容），直接报告"本次无拍摄内容"并结束。

### 第二步：摄影设计
将 scene-curator 输出的 FilmBrief 交给 cinematographer sub-agent，让它设计 SegmentPlan。

收到 SegmentPlan 后检查硬约束：
- 总段数 ≤ 8
- 总时长 ≤ 60 秒
如果超出约束，要求 cinematographer 精简。

### 第三步：执行视频生成
收集 SegmentPlan 中所有角色的衣橱数据（通过 MCP read_wardrobe 工具读取各角色衣橱），然后调用 execute_filming_pipeline 工具执行确定性管线。

传入参数：
- segment_plan_json: cinematographer 输出的 SegmentPlan JSON
- wardrobe_data_json: 衣橱数据 JSON
- film_brief_summary: FilmBrief 的叙事概要
- time_range_start: FilmBrief 中的事件时间范围起点
- time_range_end: FilmBrief 中的事件时间范围终点

## 注意
- 每个 sub-agent 有独立上下文，你需要把上一个 sub-agent 的完整输出传递给下一个
- 如果某个环节失败，报告错误原因并停止，不要重试
- 拍摄完成后汇总报告：成片路径、时长、涉及角色、段数
"""


def _build_filming_prompt() -> str:
    """构建包含去重信息的拍摄 prompt"""
    from filming_custom.filming_log import (
        format_filmed_ranges_for_prompt,
        get_recent_filmings,
    )

    recent = get_recent_filmings(hours=48)
    dedup_text = format_filmed_ranges_for_prompt(recent)

    if dedup_text:
        dedup_section = dedup_text
        dedup_instruction = "把上述已拍摄的时间范围信息传递给 scene-curator，让它在选题时跳过这些时段。"
    else:
        dedup_section = ""
        dedup_instruction = ""

    return FILMING_PROMPT_TEMPLATE.format(
        dedup_section=dedup_section,
        dedup_instruction=dedup_instruction,
    )


def run_once(thread_id: str | None = None) -> str | None:
    """执行一次完整的拍摄流程"""
    from deerflow.client import DeerFlowClient
    _ensure_subagents_registered()

    client = DeerFlowClient(
        config_path=str(Path(__file__).parent / "config.yaml"),
    )

    if thread_id is None:
        thread_id = f"filming-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    logger.info("开始拍摄流程: thread_id=%s", thread_id)

    try:
        prompt = _build_filming_prompt()
        result = client.chat(
            message=prompt,
            thread_id=thread_id,
        )
        logger.info("拍摄流程完成: %s", result[:200] if result else "(empty)")
        return result
    except Exception:
        logger.exception("拍摄流程失败")
        return None


def run_schedule(interval_hours: float = 3.0) -> None:
    """定时调度：每 N 小时触发一次拍摄"""
    logger.info("启动定时调度: 每 %.1f 小时", interval_hours)
    interval_seconds = interval_hours * 3600

    while True:
        try:
            run_once()
        except Exception:
            logger.exception("本轮拍摄异常，等待下一轮")

        logger.info("等待 %.1f 小时后执行下一轮...", interval_hours)
        time.sleep(interval_seconds)


def run_curate() -> None:
    """只运行选题（调试用）"""
    from deerflow.client import DeerFlowClient
    _ensure_subagents_registered()

    client = DeerFlowClient(
        config_path=str(Path(__file__).parent / "config.yaml"),
    )

    thread_id = f"curate-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    result = client.chat(
        message="请委派 scene-curator sub-agent 查看最近 4 小时的世界事件，筛选值得拍摄的内容。返回 FilmBrief 或 skip 决定。",
        thread_id=thread_id,
    )
    print(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="生活拍摄系统")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("once", help="单次拍摄")
    sched = sub.add_parser("schedule", help="定时调度")
    sched.add_argument("--interval", type=float, default=3.0, help="调度间隔（小时）")
    sub.add_parser("curate", help="只运行选题（调试）")

    args = parser.parse_args()

    if args.command == "once":
        run_once()
    elif args.command == "schedule":
        run_schedule(args.interval)
    elif args.command == "curate":
        run_curate()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
