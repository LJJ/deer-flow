"""拍摄记录持久化 — 记录已完成的拍摄任务，支持去重查询

记录文件：.openfang/world/films/filming_log.jsonl
每行一条 JSON 记录，包含时间范围、角色、成片路径等。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def _log_path(openfang_home: str | None = None) -> Path:
    home = openfang_home or os.environ.get("OPENFANG_HOME", "../.openfang")
    return Path(home) / "world" / "films" / "filming_log.jsonl"


def record_filming(
    time_range_start: str,
    time_range_end: str,
    characters: list[str],
    video_path: str,
    narrative_summary: str = "",
    openfang_home: str | None = None,
) -> None:
    """追加一条拍摄完成记录"""
    log_file = _log_path(openfang_home)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "time_range_start": time_range_start,
        "time_range_end": time_range_end,
        "characters": characters,
        "video_path": video_path,
        "narrative_summary": narrative_summary,
        "filmed_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info("filming_recorded: %s ~ %s, characters=%s", time_range_start, time_range_end, characters)


def get_recent_filmings(hours: float = 48, openfang_home: str | None = None) -> list[dict]:
    """读取最近 N 小时内的拍摄记录"""
    log_file = _log_path(openfang_home)
    if not log_file.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    records = []

    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            filmed_at = datetime.fromisoformat(record["filmed_at"])
            if filmed_at >= cutoff:
                records.append(record)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    return records


def format_filmed_ranges_for_prompt(records: list[dict]) -> str:
    """将拍摄记录格式化为可注入 prompt 的文本"""
    if not records:
        return ""

    lines = ["以下时间段已经拍摄过，选题时请跳过这些时间范围内的事件："]
    for r in records:
        chars = ", ".join(r.get("characters", []))
        summary = r.get("narrative_summary", "")
        lines.append(f"- {r['time_range_start']} ~ {r['time_range_end']}（角色：{chars}）{summary}")

    return "\n".join(lines)
