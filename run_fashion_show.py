#!/usr/bin/env python3
"""一次性脚本：强制从 2026-04-06 事件文件拍时装秀"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
os.chdir(Path(__file__).parent)

# 加载 .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

from run import (
    _start_trace, _end_trace, step_screenplay, step_cinematography,
    step_execute, step_execute_via_media_service, read_wardrobe,
    _deliver_to_discord, cleanup_oss_uploads, VIDEO_PROVIDER,
    logger, OPENFANG_HOME,
)

def read_events_from_file(date_str: str, since: str) -> list[dict]:
    """直接从 JSONL 文件读事件"""
    file_path = os.path.join(OPENFANG_HOME, "world", "events", f"{date_str}.jsonl")
    if not os.path.exists(file_path):
        return []
    events = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("ts", "") >= since:
                    events.append(e)
            except json.JSONDecodeError:
                pass
    return events

def main():
    # 读 4/6 深夜的事件（23:00+ 北京时间）
    events = read_events_from_file("2026-04-06", "2026-04-06T23:00")
    logger.info("从 2026-04-06.jsonl 读取到 %d 条事件（23:00后）", len(events))

    if not events:
        logger.info("无事件")
        return

    _start_trace()

    # 跳过去重检查
    import run as _run_mod
    _run_mod._get_dedup_context = lambda: ""

    # 编剧
    screenplay = step_screenplay(events)
    if not screenplay:
        return

    # 衣橱
    characters = screenplay.get("characters", [])
    wardrobe_data = {}
    wardrobe_summary = {}
    for char_id in characters:
        wd = read_wardrobe(char_id)
        wardrobe_data[char_id] = wd
        items = wd.get("items", {})
        wardrobe_summary[char_id] = [
            {"id": iid, "name": item.get("name", "")[:40]}
            for iid, item in items.items()
            if item.get("element_id")
        ]

    # 摄影设计
    segment_plan = step_cinematography(screenplay, wardrobe_summary)
    if not segment_plan:
        return

    # 执行
    logger.info("执行 pipeline (provider=%s)...", VIDEO_PROVIDER)
    try:
        if VIDEO_PROVIDER == "kling":
            result = step_execute(segment_plan, wardrobe_data, screenplay)
        else:
            result = step_execute_via_media_service(segment_plan, screenplay)
        logger.info("=== 拍摄完成 === %s", json.dumps(result, ensure_ascii=False)[:300])

        if result.get("success") and result.get("video_path"):
            _deliver_to_discord(result["video_path"], screenplay.get("logline", ""))

        _end_trace("completed")
    except Exception:
        logger.exception("pipeline 执行失败")
        _end_trace("error")
    finally:
        cleanup_oss_uploads()

if __name__ == "__main__":
    main()
