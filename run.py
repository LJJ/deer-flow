#!/usr/bin/env python3
"""生活拍摄系统入口 — 脚本编排 + LLM 调用 + 确定性 pipeline

使用方式：
  python run.py once                    # 单次拍摄
  python run.py schedule --interval 3   # 定时调度
  python run.py curate                  # 只运行选题（调试）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 确保自定义模块可导入
sys.path.insert(0, str(Path(__file__).parent / "src"))
os.chdir(Path(__file__).parent)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("filming")

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

OPENFANG_HOME = os.environ.get("OPENFANG_HOME", str(Path(__file__).parent.parent / ".openfang"))
SKILLS_DIR = Path(__file__).parent / "skills" / "custom"

# ── Skill 加载 ──────────────────────────────────────────────────────

def _load_skill(name: str) -> str:
    """读取 skill 文件内容（去掉 frontmatter）"""
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        logger.warning("skill not found: %s", path)
        return ""
    text = path.read_text("utf-8")
    # 去掉 YAML frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            text = text[end + 3:].strip()
    return text


# ── 数据读取（通过 Node.js 调 OpenFang MCP 模块）─────────────────

def _node_eval(script: str) -> str:
    """执行 Node.js 脚本，返回 stdout"""
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True,
        cwd=str(Path(OPENFANG_HOME).parent),
        env={**os.environ, "OPENFANG_HOME": OPENFANG_HOME},
    )
    if result.returncode != 0:
        logger.error("node eval failed: %s", result.stderr[:300])
        return ""
    return result.stdout.strip()


def read_world_events(since: str, limit: int = 200) -> list[dict]:
    """读取世界事件"""
    raw = _node_eval(f'''
const {{ readWorldEvents }} = require("./mcp/toolbox-mcp/tools/world_events");
const events = readWorldEvents({{ since: "{since}", limit: {limit} }});
console.log(JSON.stringify(events));
''')
    return json.loads(raw) if raw else []


def read_wardrobe(agent_name: str) -> dict:
    """读取角色衣橱 manifest"""
    raw = _node_eval(f'''
const path = require("path"), fs = require("fs");
const mf = path.join(process.env.OPENFANG_HOME, "agents", "{agent_name}", "wardrobe", "manifest.json");
if (fs.existsSync(mf)) console.log(fs.readFileSync(mf, "utf-8"));
else console.log("{{}}");
''')
    return json.loads(raw) if raw else {}


# ── LLM 调用 ────────────────────────────────────────────────────────

def _call_llm(system: str, user: str) -> str:
    """调用 LLM（使用 .env 中的 OPENAI 配置）"""
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://zaoci-02-gpt-east-us2.openai.azure.com/openai/v1")
    model = os.environ.get("FILMING_MODEL", "gpt-5.4")

    resp = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.7,
            "max_completion_tokens": 8192,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON"""
    # 先试 markdown 代码块
    m = re.search(r"```(?:json)?\s*\n([\s\S]+?)\n```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 再试裸 JSON
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


# ── 亲密场景过滤 ─────────────────────────────────────────────────

INTIMATE_FILTER = os.environ.get("FILMING_FILTER_INTIMATE", "true").lower() in ("true", "1", "yes")

INTIMATE_FILTER_INSTRUCTION = """
亲密场景过滤已开启。直接跳过以下事件：
- 亲吻、拥抱、肢体亲密接触
- 卧室内的亲密互动
- 任何带有情色暗示的对话或动作描写
- 洗澡、换衣服等涉及裸露的场景
如果所有事件都是亲密场景，输出 {"skip": true, "reason": "当前时段内容不适合拍摄"}
"""


# ── Discord 投递 ─────────────────────────────────────────────────

def _deliver_to_discord(video_path: str, caption: str = "") -> None:
    """将成片投递到 Discord 世界频道"""
    import httpx

    gateway_url = os.environ.get("DISCORD_GATEWAY_URL", "http://127.0.0.1:4320")
    gateway_token = os.environ.get("GATEWAY_TOKEN", os.environ.get("DISCORD_GATEWAY_TOKEN", ""))
    world_channel_id = os.environ.get("DISCORD_WORLD_CHANNEL_ID", "")

    if not world_channel_id:
        logger.warning("DISCORD_WORLD_CHANNEL_ID not set, skipping delivery")
        return

    try:
        import subprocess as _sp
        import tempfile as _tf

        headers = {"Content-Type": "application/json"}
        if gateway_token:
            headers["Authorization"] = f"Bearer {gateway_token}"

        # Discord 非 Nitro 限制 8MB，压缩后投递
        send_path = video_path
        file_size = os.path.getsize(video_path)
        if file_size > 7 * 1024 * 1024:  # > 7MB 就压缩
            compressed = _tf.mktemp(suffix=".mp4", prefix="film_discord_")
            _sp.run([
                "ffmpeg", "-y", "-i", video_path,
                "-c:v", "libx264", "-crf", "28", "-preset", "fast",
                "-c:a", "aac", "-b:a", "64k", compressed,
            ], check=True, capture_output=True)
            send_path = compressed
            logger.info("视频压缩: %dMB → %dMB", file_size // (1024*1024), os.path.getsize(compressed) // (1024*1024))

        resp = httpx.post(
            f"{gateway_url}/api/messages/send-video",
            json={"receive_id": world_channel_id, "video_path": send_path},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        logger.info("视频已投递到世界频道: %s", send_path)

        # 再发说明文字（如果有）
        if caption:
            httpx.post(
                f"{gateway_url}/api/messages/send",
                json={"receive_id": world_channel_id, "text": f"🎬 {caption}"},
                headers=headers,
                timeout=10,
            )
    except Exception as e:
        logger.warning("Discord 投递失败（不影响拍摄结果）: %s", e)


# ── 去重 ─────────────────────────────────────────────────────────

def _get_dedup_context() -> str:
    from filming_custom.filming_log import get_recent_filmings, format_filmed_ranges_for_prompt
    recent = get_recent_filmings(hours=48)
    return format_filmed_ranges_for_prompt(recent) or ""


# ── 第一步：编剧 ─────────────────────────────────────────────────

def step_screenplay(events: list[dict]) -> dict | None:
    """调 LLM 选题+编剧，直接输出完整剧本"""
    skill = _load_skill("screenplay")
    dedup = _get_dedup_context()

    system = f"你是拍摄系统的编剧。从角色的生活事件中选择值得拍的内容，写成完整的视频剧本。\n\n{skill}"
    if INTIMATE_FILTER:
        system += f"\n{INTIMATE_FILTER_INSTRUCTION}"

    # 均匀采样，覆盖全天
    if len(events) > 80:
        step = len(events) / 80
        sampled = [events[int(i * step)] for i in range(80)]
    else:
        sampled = events

    events_text = json.dumps(sampled, ensure_ascii=False)
    user = f"以下是今天的世界事件（{len(events)} 条中采样 {len(sampled)} 条，覆盖全天）：\n{events_text}"
    if dedup:
        user += f"\n\n已拍摄过的时间段（请跳过）：\n{dedup}"

    logger.info("编剧: %d events, calling LLM...", len(sampled))
    result_text = _call_llm(system, user)
    result = _extract_json(result_text)

    if not result:
        logger.error("编剧返回无法解析: %s", result_text[:300])
        return None

    if result.get("skip"):
        logger.info("编剧: skip — %s", result.get("reason", ""))
        return None

    logger.info("编剧完成: %s | %d segments", result.get("logline", "")[:80], len(result.get("segments", [])))
    return result


# ── 第二步：摄影设计 ──────────────────────────────────────────────

def step_cinematography(screenplay: dict, wardrobe_summary: dict) -> dict | None:
    """调 LLM 按剧本设计分镜，输出 SegmentPlan"""
    cinematography_skill = _load_skill("cinematography")
    kling_skill = _load_skill("kling-constraints")

    system = f"你是拍摄系统的摄影师。根据编剧写好的剧本，设计每段的具体分镜。\n\n{cinematography_skill}\n\n{kling_skill}"

    user = f"""编剧剧本：
{json.dumps(screenplay, ensure_ascii=False, indent=2)}

各角色衣橱（用于匹配 outfit_item_id）:
{json.dumps(wardrobe_summary, ensure_ascii=False, indent=2)}

按剧本设计每段分镜，输出 SegmentPlan JSON。"""

    logger.info("摄影设计: calling LLM...")
    result_text = _call_llm(system, user)
    result = _extract_json(result_text)

    if not result or "segments" not in result:
        logger.error("摄影设计返回无效: %s", result_text[:500])
        return None

    logger.info("摄影设计完成: %d segments", len(result["segments"]))
    return result


# ── 第三步：执行 ─────────────────────────────────────────────────

MEDIA_SERVICE_URL = os.environ.get("MEDIA_SERVICE_URL", "http://127.0.0.1:4500")
VIDEO_PROVIDER = os.environ.get("VIDEO_PROVIDER", "kling")


def step_execute_via_media_service(segment_plan: dict, screenplay: dict) -> dict:
    """通过 media-service HTTP API 生成视频（不依赖内部 kling_client，支持任意 provider）"""
    import httpx
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    segments = segment_plan.get("segments", [])
    if not segments:
        return {"success": False, "error": "no segments"}

    provider = VIDEO_PROVIDER
    tmp_dir = tempfile.mkdtemp(prefix="filming_")

    try:
        # 提交所有段（并行提交，串行轮询）
        tasks = []
        for seg in segments:
            # compose prompt: scene_description + shots（和 pipeline.py 的 compose_prompt 逻辑一致）
            lines = [seg.get("scene_description", ""), ""]
            for shot in seg.get("shots", []):
                dur = f"{int(shot.get('duration_seconds', 5))}s"
                lines.append(f"镜头{shot.get('shot_index', 0) + 1}，{dur}，{shot.get('scale', '')}，{shot.get('shot_prompt', '')}")

            # 视角约束
            perspective = seg.get("perspective", "first_person")
            lines.append("")
            if perspective == "first_person":
                lines.append("第一人称视角，镜头是拍摄者的眼睛。角色看向镜头表示与拍摄者对话。画面中只出现上述角色，不出现其他任何人。")
            else:
                lines.append("电影镜头视角，角色之间自然互动，不看镜头。画面中只出现上述角色，不出现其他任何人。")

            prompt = "\n".join(lines)

            body = {
                "provider": provider,
                "prompt": prompt,
                "duration": int(seg.get("duration_seconds", 10)),
                "aspect_ratio": seg.get("aspect_ratio", "16:9"),
            }

            logger.info("提交 segment %d via %s (%ds)", seg.get("segment_index", 0), provider, body["duration"])
            resp = httpx.post(f"{MEDIA_SERVICE_URL}/video/generate", json=body, timeout=30)
            resp.raise_for_status()
            task_info = resp.json()
            tasks.append({"seg": seg, "taskId": task_info["taskId"], "provider": task_info["provider"], "prompt": prompt})

        # 轮询所有任务
        video_paths = []
        prompts = []
        for task in tasks:
            task_id = task["taskId"]
            task_provider = task["provider"]
            logger.info("轮询 segment %d task=%s", task["seg"].get("segment_index", 0), task_id)

            for _ in range(120):  # 最多 20 分钟
                time.sleep(10)
                resp = httpx.get(f"{MEDIA_SERVICE_URL}/task/{task_provider}/{task_id}", timeout=30)
                result = resp.json()

                if result["status"] == "completed":
                    video_url = result["result"]["url"]
                    # 下载视频
                    dest = os.path.join(tmp_dir, f"segment_{task['seg'].get('segment_index', 0)}.mp4")
                    dl_resp = httpx.get(video_url, timeout=120)
                    with open(dest, "wb") as f:
                        f.write(dl_resp.content)
                    video_paths.append(dest)
                    prompts.append(task["prompt"])
                    logger.info("segment %d 完成: %s", task["seg"].get("segment_index", 0), dest)
                    break
                elif result["status"] == "failed":
                    logger.error("segment %d 失败: %s", task["seg"].get("segment_index", 0), result.get("error", ""))
                    return {"success": False, "error": result.get("error", "segment failed")}
            else:
                return {"success": False, "error": f"segment {task['seg'].get('segment_index', 0)} polling timeout"}

        if not video_paths:
            return {"success": False, "error": "no videos generated"}

        # 拼接
        final_path = os.path.join(tmp_dir, "final.mp4")
        if len(video_paths) == 1:
            shutil.copy2(video_paths[0], final_path)
        else:
            concat_file = os.path.join(tmp_dir, "concat.txt")
            with open(concat_file, "w") as f:
                for vp in video_paths:
                    f.write(f"file '{vp}'\n")
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", final_path], check=True, capture_output=True)

        # 存档
        openfang_home = os.environ.get("OPENFANG_HOME", str(Path(__file__).parent.parent / ".openfang"))
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        films_dir = Path(openfang_home) / "world" / "films" / now.strftime("%Y-%m-%d")
        films_dir.mkdir(parents=True, exist_ok=True)
        video_name = f"film_{now.strftime('%H%M%S')}.mp4"
        dest_video = films_dir / video_name
        shutil.copy2(final_path, dest_video)

        meta = {
            "video_file": video_name,
            "duration_seconds": sum(s.get("duration_seconds", 0) for s in segments),
            "segments": len(segments),
            "characters": screenplay.get("characters", []),
            "narrative_summary": screenplay.get("logline", ""),
            "provider": provider,
            "segment_prompts": prompts,
            "created_at": now.isoformat(),
        }
        meta_path = films_dir / f"film_{now.strftime('%H%M%S')}_meta.json"
        with open(meta_path, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        logger.info("archived: %s", dest_video)
        return {"success": True, "video_path": str(dest_video), "meta_path": str(meta_path)}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def step_execute(segment_plan: dict, wardrobe_data: dict, screenplay: dict) -> dict:
    """执行确定性 pipeline"""
    from filming_custom.tools import parse_segment_plan, run_pipeline

    plan_json = json.dumps(segment_plan, ensure_ascii=False)

    return run_pipeline(
        segment_plan_json=plan_json,
        wardrobe_data=wardrobe_data,
        film_brief_summary=screenplay.get("logline", ""),
        time_range_start=screenplay.get("time_range", {}).get("start", ""),
        time_range_end=screenplay.get("time_range", {}).get("end", ""),
    )


# ── 完整流程 ─────────────────────────────────────────────────────

def run_once(since: str | None = None) -> dict | None:
    """执行一次完整的拍摄流程"""
    if since is None:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    logger.info("=== 开始拍摄 (since=%s) ===", since)

    # 读数据
    events = read_world_events(since)
    logger.info("读取到 %d 个世界事件", len(events))
    if not events:
        logger.info("无事件，结束")
        return None

    # 第一步：编剧（选题+剧本）
    screenplay = step_screenplay(events)
    if not screenplay:
        return None

    # 读衣橱
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

    # 第二步：摄影设计
    segment_plan = step_cinematography(screenplay, wardrobe_summary)
    if not segment_plan:
        return None

    # 第三步：执行
    logger.info("执行 pipeline (provider=%s)...", VIDEO_PROVIDER)
    try:
        if VIDEO_PROVIDER == "kling":
            result = step_execute(segment_plan, wardrobe_data, screenplay)
        else:
            result = step_execute_via_media_service(segment_plan, screenplay)
        logger.info("=== 拍摄完成 === %s", json.dumps(result, ensure_ascii=False)[:300])

        # 第四步：投递到 Discord 世界频道
        if result.get("success") and result.get("video_path"):
            _deliver_to_discord(result["video_path"], screenplay.get("logline", ""))

        return result
    except Exception:
        logger.exception("pipeline 执行失败")
        return None


def run_schedule(interval_hours: float = 3.0) -> None:
    """定时调度"""
    logger.info("启动定时调度: 每 %.1f 小时", interval_hours)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("本轮拍摄异常")
        logger.info("等待 %.1f 小时...", interval_hours)
        time.sleep(interval_hours * 3600)


def run_curate(since: str | None = None) -> None:
    """只运行选题（调试）"""
    if since is None:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    events = read_world_events(since)
    result = step_curate(events)
    print(json.dumps(result, ensure_ascii=False, indent=2) if result else "skip")


def main() -> None:
    parser = argparse.ArgumentParser(description="生活拍摄系统")
    sub = parser.add_subparsers(dest="command")

    once_p = sub.add_parser("once", help="单次拍摄")
    once_p.add_argument("--since", help="事件起始时间 (ISO8601)")

    sched_p = sub.add_parser("schedule", help="定时调度")
    sched_p.add_argument("--interval", type=float, default=3.0, help="间隔（小时）")

    curate_p = sub.add_parser("curate", help="只运行选题")
    curate_p.add_argument("--since", help="事件起始时间 (ISO8601)")

    args = parser.parse_args()

    if args.command == "once":
        run_once(args.since)
    elif args.command == "schedule":
        run_schedule(args.interval)
    elif args.command == "curate":
        run_curate(args.since)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
