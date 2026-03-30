"""拍摄系统工具 — 供 lead agent 调用的确定性执行管线入口"""

from __future__ import annotations

import asyncio
import json
import logging

from langchain.tools import tool

from .kling_client import KlingClient
from .models import CharacterInSegment, Segment, SegmentPlan, Shot
from .pipeline import execute_pipeline

logger = logging.getLogger(__name__)


def _parse_segment_plan(plan_json: str) -> SegmentPlan:
    """从 JSON 字符串解析 SegmentPlan"""
    data = json.loads(plan_json)
    segments = []
    for seg_data in data.get("segments", []):
        characters = [
            CharacterInSegment(
                character_id=c["character_id"],
                outfit_item_id=c["outfit_item_id"],
            )
            for c in seg_data.get("characters", [])
        ]
        shots = [
            Shot(
                shot_index=s.get("shot_index", i),
                scale=s.get("scale", "中景"),
                camera_movement=s.get("camera_movement", "固定"),
                duration_seconds=s.get("duration_seconds", 5),
                shot_prompt=s.get("shot_prompt", ""),
            )
            for i, s in enumerate(seg_data.get("shots", []))
        ]
        segments.append(
            Segment(
                segment_index=seg_data.get("segment_index", 0),
                scene_description=seg_data.get("scene_description", ""),
                duration_seconds=seg_data.get("duration_seconds", 8),
                aspect_ratio=seg_data.get("aspect_ratio", "16:9"),
                transition_to_next=seg_data.get("transition_to_next", "hard_cut"),
                characters=characters,
                shots=shots,
                prompt=seg_data.get("prompt", ""),
            )
        )
    return SegmentPlan(segments=segments)


def _parse_wardrobe_data(wardrobe_json: str) -> dict:
    """从 JSON 字符串解析衣橱数据"""
    return json.loads(wardrobe_json)


@tool("execute_filming_pipeline", parse_docstring=True)
def execute_filming_pipeline_tool(
    segment_plan_json: str,
    wardrobe_data_json: str,
    film_brief_summary: str,
    time_range_start: str = "",
    time_range_end: str = "",
) -> str:
    """执行完整的视频生成管线：解析 SegmentPlan → 生成视频 → 拼接 → 存档。

    在 Scene Curator 和 Cinematographer 完成创作决策后，调用此工具执行确定性的视频生成流程。

    Args:
        segment_plan_json: Cinematographer 输出的 SegmentPlan JSON 字符串，包含 segments 数组
        wardrobe_data_json: 从 MCP read_wardrobe 获取的衣橱数据 JSON，格式为 {character_id: {items: {item_id: {element_id: "..."}}}}
        film_brief_summary: FilmBrief 的叙事概要，用于存档 meta.json
        time_range_start: FilmBrief 的事件时间范围起点（ISO8601），用于拍摄记录去重
        time_range_end: FilmBrief 的事件时间范围终点（ISO8601），用于拍摄记录去重
    """
    try:
        plan = _parse_segment_plan(segment_plan_json)

        # 校验硬约束
        errors = plan.validate_constraints()
        if errors:
            return json.dumps(
                {"success": False, "errors": errors},
                ensure_ascii=False,
            )

        wardrobe_data = _parse_wardrobe_data(wardrobe_data_json)

        # 在事件循环中执行异步管线
        result = asyncio.run(execute_pipeline(
            plan, wardrobe_data, film_brief_summary,
            time_range_start=time_range_start,
            time_range_end=time_range_end,
        ))

        return json.dumps(
            {"success": True, **result},
            ensure_ascii=False,
        )

    except Exception as e:
        logger.exception("pipeline_failed")
        return json.dumps(
            {"success": False, "error": str(e)},
            ensure_ascii=False,
        )
