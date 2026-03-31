"""拍摄系统工具 — 解析 + 执行管线（纯代码调用，不注册为 LLM tool）"""

from __future__ import annotations

import asyncio
import json
import logging

from .models import CharacterInSegment, Segment, SegmentPlan, Shot
from .pipeline import execute_pipeline

logger = logging.getLogger(__name__)


def parse_segment_plan(plan_json: str) -> SegmentPlan:
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
                perspective=seg_data.get("perspective", "first_person"),
                characters=characters,
                shots=shots,
                prompt=seg_data.get("prompt", ""),
            )
        )
    return SegmentPlan(segments=segments)


def run_pipeline(
    segment_plan_json: str,
    wardrobe_data: dict,
    film_brief_summary: str,
    time_range_start: str = "",
    time_range_end: str = "",
) -> dict:
    """执行完整的视频生成管线。由 run.py 的 lead agent 编排后调用。"""
    plan = parse_segment_plan(segment_plan_json)

    errors = plan.validate_constraints()
    if errors:
        return {"success": False, "errors": errors}

    result = asyncio.run(execute_pipeline(
        plan, wardrobe_data, film_brief_summary,
        time_range_start=time_range_start,
        time_range_end=time_range_end,
    ))
    return {"success": True, **result}
