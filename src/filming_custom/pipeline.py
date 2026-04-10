"""视频生成执行管线 — 确定性代码，不经过 LLM

链路：SegmentPlan → Element 解析 → Prompt 组装 → KlingAI 生成 → 拼接 → 存档
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .filming_log import record_filming
from .kling_client import KlingClient, download_video, poll_video, submit_video
from .models import Segment, SegmentPlan

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 4.1 Element 解析
# ---------------------------------------------------------------------------

def resolve_elements(
    segment: Segment,
    wardrobe_data: dict[str, Any],
) -> list[dict[str, str]]:
    """根据 segment 中每个角色的 outfit_item_id，从衣橱 manifest 查 element_id。

    wardrobe_data: {character_id: {items: {item_id: {element_id: "..."}}}}
    返回 KlingAI element_list 格式: [{"id": element_id, "name": character_name}]
    """
    element_list = []
    for char in segment.characters:
        if char.is_npc:
            # NPC 没有 element，外貌由 shot_prompt 文本描述
            continue
        char_wardrobe = wardrobe_data.get(char.character_id, {})
        items = char_wardrobe.get("items", {})
        item = items.get(char.outfit_item_id, {})
        element_id = item.get("element_id")
        if not element_id:
            logger.warning(
                "element_not_found: character=%s outfit_item_id=%s",
                char.character_id,
                char.outfit_item_id,
            )
            continue
        element_list.append({"element_id": str(element_id), "name": char.character_id})
    return element_list


# ---------------------------------------------------------------------------
# 4.2 Prompt 组装
# ---------------------------------------------------------------------------

CHARACTER_DISPLAY_NAMES = {
    "songyu": "宋玉",
    "ziling": "紫灵",
}


def _replace_names(text: str, element_list: list[dict[str, str]]) -> str:
    """将文本中的角色名（character_id 和中文名）替换为 <<<element_N>>> 标记。"""
    for i, elem in enumerate(element_list, 1):
        marker = f"<<<element_{i}>>>"
        cid = elem["name"]
        text = text.replace(cid, marker)
        display = CHARACTER_DISPLAY_NAMES.get(cid)
        if display:
            text = text.replace(display, marker)
    return text


def _get_npc_names(segment: Segment) -> list[str]:
    """从 segment 中提取 NPC 角色的中文名（用于 prompt 约束行）"""
    return [c.display_name or c.character_id for c in segment.characters if c.is_npc]


def compose_prompt(segment: Segment, element_list: list[dict[str, str]]) -> str:
    """将 SegmentPlan 的 shots 结构拼成 KlingAI 中文镜头格式。

    输出格式示例：
      现代都市，傍晚暖光。宋玉家客厅，阳光从窗户照进来，餐桌上摆着面包和零食。

      镜头1，3s，中景，<<<element_1>>>坐在餐桌前，手撑着头，慵懒地嚼着面包
      镜头2，4s，近景，<<<element_2>>>从纸袋里拿出巧克力面包咬了一口，眼睛亮了
    """
    lines = []

    # 场景描述行
    scene = _replace_names(segment.scene_description, element_list)
    lines.append(scene)
    lines.append("")

    # 镜头行：按 shots 数组构建
    if segment.shots:
        for shot in segment.shots:
            dur = f"{int(shot.duration_seconds)}s"
            desc = _replace_names(shot.shot_prompt, element_list)
            lines.append(f"镜头{shot.shot_index + 1}，{dur}，{shot.scale}，{desc}")
    else:
        # fallback：如果没有 shots，用 segment.prompt 整段描述
        lines.append(_replace_names(segment.prompt, element_list))

    # 根据视角追加约束
    lines.append("")
    npc_names = _get_npc_names(segment)
    npc_note = f"以及{' '.join(npc_names)}" if npc_names else ""
    if segment.perspective == "first_person":
        lines.append(f"第一人称视角，镜头是拍摄者的眼睛。角色看向镜头表示与拍摄者对话。画面中只出现上述角色{npc_note}，不出现其他任何人。")
    else:
        lines.append(f"电影镜头视角，角色之间自然互动，不看镜头。画面中只出现上述角色{npc_note}，不出现其他任何人。")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4.4-4.5 视频生成（单段 + 多段）
# ---------------------------------------------------------------------------

async def generate_single_segment(
    client: KlingClient,
    segment: Segment,
    element_list: list[dict[str, str]],
    first_frame_url: str | None = None,
    output_dir: str = "/tmp/filming",
) -> tuple[str, str]:
    """生成单段视频，返回 (本地视频文件路径, 发给 KlingAI 的 prompt)"""
    prompt = compose_prompt(segment, element_list)

    task_id = await submit_video(
        client=client,
        prompt=prompt,
        element_list=element_list,
        aspect_ratio=segment.aspect_ratio,
        duration_seconds=int(segment.duration_seconds),
        first_frame_url=first_frame_url,
    )

    result = await poll_video(client, task_id)

    # 从结果中提取视频 URL
    works = result.get("task_result", {}).get("videos", [])
    if not works:
        raise RuntimeError(f"KlingAI 返回无视频: task_id={task_id}")
    video_url = works[0].get("url", "")
    if not video_url:
        raise RuntimeError(f"KlingAI 视频 URL 为空: task_id={task_id}")

    # 下载到本地
    os.makedirs(output_dir, exist_ok=True)
    dest = os.path.join(output_dir, f"segment_{segment.segment_index}.mp4")
    await download_video(video_url, dest)
    return dest, prompt


def extract_last_frame(video_path: str, output_path: str) -> str:
    """用 ffmpeg 从视频提取末帧，返回帧图片路径"""
    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-0.1",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    logger.info("extracted_last_frame: %s -> %s", video_path, output_path)
    return output_path


async def generate_all_segments(
    client: KlingClient,
    plan: SegmentPlan,
    wardrobe_data: dict[str, Any],
    output_dir: str | None = None,
) -> tuple[list[str], list[str]]:
    """按 SegmentPlan 生成所有段的视频。

    根据 transition_to_next 决定串行（first_frame 传递）或并行。
    返回 (视频路径列表, prompt 列表)。
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="filming_")

    segments = plan.segments
    video_paths: list[str | None] = [None] * len(segments)
    prompts: list[str] = [""] * len(segments)

    # 构建 chain 列表：连续 first_frame 的段组成一条 chain，不同 chain 可并行
    chains: list[list[int]] = []
    i = 0
    while i < len(segments):
        chain = [i]
        j = i
        while j < len(segments) - 1 and segments[j].transition_to_next == "first_frame":
            chain.append(j + 1)
            j += 1
        chains.append(chain)
        i = j + 1

    async def _run_chain(chain: list[int]) -> None:
        first_frame_url = None
        for seg_idx in chain:
            seg = segments[seg_idx]
            element_list = resolve_elements(seg, wardrobe_data)
            path, prompt = await generate_single_segment(
                client=client,
                segment=seg,
                element_list=element_list,
                first_frame_url=first_frame_url,
                output_dir=output_dir,
            )
            video_paths[seg_idx] = path
            prompts[seg_idx] = prompt

            if seg.transition_to_next == "first_frame" and seg_idx < len(segments) - 1:
                frame_path = os.path.join(output_dir, f"frame_{seg_idx}.jpg")
                extract_last_frame(path, frame_path)
                first_frame_url = frame_path
            else:
                first_frame_url = None

    # 并行执行所有独立 chain
    await asyncio.gather(*[_run_chain(c) for c in chains])

    valid = [(p, pr) for p, pr in zip(video_paths, prompts) if p is not None]
    return [v[0] for v in valid], [v[1] for v in valid]


# ---------------------------------------------------------------------------
# 4.6 视频拼接
# ---------------------------------------------------------------------------

def concat_videos(video_paths: list[str], output_path: str) -> str:
    """使用 ffmpeg concat 拼接多段视频为成片"""
    if len(video_paths) == 1:
        shutil.copy2(video_paths[0], output_path)
        return output_path

    # 创建 concat 文件列表
    concat_file = output_path + ".concat.txt"
    with open(concat_file, "w") as f:
        for p in video_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    os.unlink(concat_file)
    logger.info("concat_done: %d segments -> %s", len(video_paths), output_path)
    return output_path


# ---------------------------------------------------------------------------
# 4.7 存档
# ---------------------------------------------------------------------------

def archive_film(
    video_path: str,
    plan: SegmentPlan,
    film_brief_summary: str,
    time_range_start: str = "",
    time_range_end: str = "",
    segment_prompts: list[str] | None = None,
    openfang_home: str | None = None,
) -> dict[str, str]:
    """将成片存入 .openfang/world/films/YYYY-MM-DD/，附带 meta.json，并写入拍摄记录。

    返回 {"video_path": ..., "meta_path": ...}
    """
    if openfang_home is None:
        openfang_home = os.environ.get("OPENFANG_HOME", "../.openfang")

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    films_dir = Path(openfang_home) / "world" / "films" / date_str
    films_dir.mkdir(parents=True, exist_ok=True)

    # 复制视频
    video_name = f"film_{time_str}.mp4"
    dest_video = films_dir / video_name
    shutil.copy2(video_path, dest_video)

    # 生成 meta.json
    all_characters = set()
    all_locations = set()
    for seg in plan.segments:
        for c in seg.characters:
            all_characters.add(c.character_id)
        all_locations.add(seg.scene_description)

    characters_sorted = sorted(all_characters)

    meta = {
        "video_file": video_name,
        "duration_seconds": plan.total_duration,
        "segments": plan.total_segments,
        "characters": characters_sorted,
        "locations": sorted(all_locations),
        "narrative_summary": film_brief_summary,
        "time_range_start": time_range_start,
        "time_range_end": time_range_end,
        "created_at": now.isoformat(),
        "segment_prompts": segment_prompts or [],
    }

    meta_path = films_dir / f"film_{time_str}_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 写入拍摄记录（供去重查询）
    if time_range_start and time_range_end:
        record_filming(
            time_range_start=time_range_start,
            time_range_end=time_range_end,
            characters=characters_sorted,
            video_path=str(dest_video),
            narrative_summary=film_brief_summary,
            openfang_home=openfang_home,
        )

    logger.info("archived: %s + %s", dest_video, meta_path)
    return {"video_path": str(dest_video), "meta_path": str(meta_path)}


# ---------------------------------------------------------------------------
# 完整管线入口
# ---------------------------------------------------------------------------

async def execute_pipeline(
    plan: SegmentPlan,
    wardrobe_data: dict[str, Any],
    film_brief_summary: str,
    time_range_start: str = "",
    time_range_end: str = "",
    kling_client: KlingClient | None = None,
) -> dict[str, str]:
    """执行完整的视频生成管线：生成 → 拼接 → 存档"""
    client = kling_client or KlingClient()
    tmp_dir = tempfile.mkdtemp(prefix="filming_")

    try:
        # 生成所有段
        video_paths, segment_prompts = await generate_all_segments(client, plan, wardrobe_data, tmp_dir)

        if not video_paths:
            raise RuntimeError("没有生成任何视频段")

        # 拼接
        final_path = os.path.join(tmp_dir, "final.mp4")
        concat_videos(video_paths, final_path)

        # 存档 + 写入拍摄记录
        result = archive_film(
            final_path, plan, film_brief_summary,
            time_range_start=time_range_start,
            time_range_end=time_range_end,
            segment_prompts=segment_prompts,
        )
        return result

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if kling_client is None:
            await client.close()
