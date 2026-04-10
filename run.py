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
OPENFANG_API = os.environ.get("OPENFANG_API", "http://127.0.0.1:4200")


# ── Tracing ──────────────────────────────────────────────────────

import uuid as _uuid

_current_trace_id: str | None = None


def _start_trace() -> str:
    """创建 filming trace，返回 trace_id"""
    global _current_trace_id
    _current_trace_id = f"filming-{_uuid.uuid4().hex[:12]}"
    logger.info("trace: %s", _current_trace_id)
    return _current_trace_id


def _end_trace(status: str = "completed") -> None:
    """结束当前 trace（通过发送特殊 span）"""
    if not _current_trace_id:
        return
    try:
        import httpx
        httpx.post(f"{OPENFANG_API}/api/traces/{_current_trace_id}/spans",
                   json={"name": "_trace_complete", "status": status, "kind": "custom",
                         "started_at": datetime.now(timezone.utc).isoformat()},
                   timeout=5)
    except Exception:
        pass


def _report_span(name: str, kind: str = "custom", input_text: str = "", output_text: str = "",
                 duration_ms: int | None = None, error: str | None = None, metadata: dict | None = None) -> None:
    """上报 span 到 OpenFang trace 系统"""
    if not _current_trace_id:
        return
    try:
        import httpx
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "name": f"filming:{name}",
            "kind": kind,
            "started_at": now,
            "ended_at": now,
            "duration_ms": duration_ms,
            "input": input_text[:10000] if input_text else None,
            "output": (f"[error] {error}" if error else output_text[:10000]) if (error or output_text) else None,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
        }
        httpx.post(f"{OPENFANG_API}/api/traces/{_current_trace_id}/spans",
                   json=body, timeout=5)
    except Exception:
        pass  # fire-and-forget

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


def read_npc_visuals(npc_ids: list[str] | None = None) -> dict:
    """读取 NPC 外貌描述"""
    ids_arg = json.dumps(npc_ids) if npc_ids else "null"
    raw = _node_eval(f'''
const {{ loadNpcRegistry, resolveNpcVisual }} = require("./mcp/toolbox-mcp/tools/npc");
const registry = loadNpcRegistry();
const npcIds = {ids_arg} || Object.keys(registry);
const result = {{}};
for (const npcId of npcIds) {{
  const npc = registry[npcId];
  if (!npc || (npc.status || "active") !== "active") continue;
  const visual = resolveNpcVisual(npc);
  result[npcId] = {{ name: npc.name, visual, speech_style: npc.speech_style || "" }};
}}
console.log(JSON.stringify(result));
''')
    return json.loads(raw) if raw else {}


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

def _resolve_filming_chain() -> list[str]:
    """从 llm_routing.json 读取 filming 模型链 [primary, fallback, fallback2]"""
    import json as _json
    routing_path = os.path.join(
        os.environ.get("OPENFANG_HOME", os.path.expanduser("~/.openfang")),
        "llm_routing.json",
    )
    default = os.environ.get("FILMING_MODEL", "gpt-5.4")
    try:
        with open(routing_path) as f:
            slot = _json.load(f).get("slots", {}).get("filming", {})
        chain = []
        seen = set()
        for key in ("primary", "fallback", "fallback2"):
            m = slot.get(key)
            if m and m not in seen:
                seen.add(m)
                chain.append(m)
        return chain if chain else [default]
    except Exception:
        return [default]


def _call_llm(system: str, user: str) -> str:
    """调用 LLM，按 llm_routing.json 的 filming chain 逐个尝试"""
    import httpx
    import re

    chain = _resolve_filming_chain()
    last_err = None

    for model in chain:
        # 供应商路由（同 MCP 层逻辑）
        if model.startswith("deepseek"):
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        elif model.startswith("MiniMax") or model.startswith("M2"):
            api_key = os.environ.get("MINIMAX_API_KEY", "")
            base_url = "https://api.minimaxi.chat/v1"
        elif model.startswith("gpt-"):
            api_key = os.environ.get("AZURE_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
            base_url = os.environ.get("AZURE_OPENAI_BASE_URL", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
        else:
            api_key = os.environ.get("OPENAI_API_KEY", "")
            base_url = os.environ.get("OPENAI_BASE_URL", "https://vibecodingapi.ai/v1")

        token_key = "max_completion_tokens" if model.startswith("gpt-5") else "max_tokens"
        url = f"{base_url}/chat/completions"
        logger.info("_call_llm: model=%s base_url=%s system=%d user=%d", model, base_url, len(system), len(user))
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "temperature": 0.7,
                    token_key: 8192,
                },
                timeout=300,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            # Strip <think> tags (thinking model compat)
            content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
            finish = body["choices"][0].get("finish_reason", "?")
            comp_tokens = body.get("usage", {}).get("completion_tokens", "?")
            logger.info("_call_llm: finish=%s content_len=%d completion_tokens=%s", finish, len(content), comp_tokens)
            return content
        except Exception as e:
            last_err = e
            logger.warning("_call_llm: %s failed (%s), trying next", model, e)
            continue

    raise last_err or RuntimeError("All models in filming chain failed")


def _fix_json_quotes(text: str) -> str:
    """修复 LLM 输出中 JSON 字符串值内部的未转义双引号。
    策略：找到非 JSON 结构性的 "xxx" 引号对，替换为「xxx」。
    结构性引号特征：紧跟 : [ ] { } , 或在行首。非结构性引号：出现在叙事文本中间。
    """
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            result.append(ch)
            escape = False
            i += 1
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            i += 1
            continue
        if ch == '"':
            if not in_string:
                # 开始字符串
                in_string = True
                result.append(ch)
            else:
                # 可能是字符串结束，也可能是内嵌引号
                # 看后面：如果紧跟 JSON 结构字符（: , } ]）或空白+结构字符，是真结束
                rest = text[i+1:].lstrip()
                if not rest or rest[0] in ':,}]\n':
                    in_string = False
                    result.append(ch)
                else:
                    # 内嵌引号，替换为「」
                    # 找配对的关闭引号
                    close = text.find('"', i + 1)
                    if close > 0:
                        after_close = text[close+1:].lstrip()
                        if after_close and after_close[0] not in ':,}]\n':
                            # 关闭引号后面也不是结构字符，两个都是内嵌引号
                            result.append('「')
                            result.append(text[i+1:close])
                            result.append('」')
                            i = close + 1
                            continue
                    result.append('「')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON"""
    # 先试 markdown 代码块
    m = re.search(r"```(?:json)?\s*\n([\s\S]+?)\n```", text)
    if m:
        raw = m.group(1)
        for attempt in (raw, _fix_json_quotes(raw)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    # 再试裸 JSON
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        raw = m.group()
        for attempt in (raw, _fix_json_quotes(raw)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    return None


# ── 亲密场景过滤 ─────────────────────────────────────────────────

INTIMATE_FILTER = os.environ.get("FILMING_FILTER_INTIMATE", "true").lower() in ("true", "1", "yes")

INTIMATE_FILTER_INSTRUCTION = """
亲密场景过滤已开启。跳过以下事件：
- 明确的性行为描写
- 完全裸露的场景
拥抱、换装、亲密互动、微醺、肢体接触等日常亲密场景可以正常选入。
如果所有事件都是需要跳过的类型，输出 {"skip": true, "reason": "当前时段内容不适合拍摄"}
"""


# ── OSS 上传 ─────────────────────────────────────────────────────

OSS_UPLOAD_BASE_URL = os.environ.get("OSS_UPLOAD_BASE_URL", "http://127.0.0.1:4390")
OSS_UPLOAD_TOKEN = os.environ.get("OSS_UPLOAD_TOKEN", "")
_oss_uploaded_files: list[str] = []  # 跟踪上传的文件，pipeline 结束后清理


def upload_to_oss(file_path: str, kind: str = "images") -> str:
    """上传本地文件到 OSS，返回 public URL"""
    import httpx

    with open(file_path, "rb") as f:
        file_data = f.read()

    # 用时间戳+随机后缀避免文件名冲突
    import hashlib
    ext = os.path.splitext(file_path)[1] or ".bin"
    filename = f"{hashlib.md5(file_path.encode()).hexdigest()[:12]}{ext}"
    files = {"file": (filename, file_data, "application/octet-stream")}
    data = {"kind": kind, "filename": filename}
    headers = {}
    if OSS_UPLOAD_TOKEN:
        headers["Authorization"] = f"Bearer {OSS_UPLOAD_TOKEN}"

    resp = httpx.post(
        f"{OSS_UPLOAD_BASE_URL}/api/v1/uploads/files",
        files=files,
        data=data,
        headers=headers,
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"OSS upload failed: {result.get('error', '')}")

    url = result["data"].get("url", "")
    stored_path = result["data"].get("stored_path", "")
    # 替换本地地址为公网地址
    public_base = os.environ.get("OSS_PUBLIC_BASE_URL", "http://www.lujiji.com")
    url = url.replace(OSS_UPLOAD_BASE_URL, public_base)
    # 记录用于后续清理
    if stored_path:
        _oss_uploaded_files.append(stored_path)
    logger.info("OSS 上传完成: %s → %s", file_path, url)
    return url


def cleanup_oss_uploads() -> None:
    """清理本次拍摄上传的临时文件"""
    import httpx
    if not _oss_uploaded_files:
        return
    headers = {}
    if OSS_UPLOAD_TOKEN:
        headers["Authorization"] = f"Bearer {OSS_UPLOAD_TOKEN}"
    for stored_path in _oss_uploaded_files:
        try:
            httpx.delete(
                f"{OSS_UPLOAD_BASE_URL}/api/v1/uploads{stored_path}",
                headers=headers,
                timeout=10,
            )
        except Exception:
            pass  # best effort
    logger.info("OSS 清理: %d 个临时文件", len(_oss_uploaded_files))
    _oss_uploaded_files.clear()


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

_EXPLICIT_KEYWORDS = re.compile(
    r"骑在.*身上|高潮|夹[紧得]|喉咙里.*吟|收缩|插入|抽[送插]|射[了在]|精液|阴[道茎蒂]|乳[头首房]|奶[子头]|"
    r"裸[体露]|光[着了]身|脱[光掉].*衣|硬[了起]|勃起|潮[吹湿]|呻吟"
)


def _filter_explicit(events: list[dict]) -> list[dict]:
    """过滤掉包含明确性行为描写的事件，防止模型拒绝输出"""
    return [e for e in events if not _EXPLICIT_KEYWORDS.search(e.get("content", ""))]


def _segment_events(events: list[dict], gap_minutes: int = 45) -> list[dict]:
    """用代码按时间间隔自动分段，返回摘要列表。只按大的时间间隔切，不按地点切。"""
    if not events:
        return []

    def _parse_ts(ts: str):
        from datetime import datetime
        return datetime.fromisoformat(ts)

    segments = []
    cur_events = [events[0]]

    for e in events[1:]:
        prev_ts = _parse_ts(cur_events[-1]["ts"])
        cur_ts = _parse_ts(e["ts"])
        gap = (cur_ts - prev_ts).total_seconds() / 60
        if gap > gap_minutes:
            segments.append(cur_events)
            cur_events = [e]
        else:
            cur_events.append(e)

    if cur_events:
        segments.append(cur_events)

    # 生成摘要（保留原始事件引用）
    summaries = []
    _segment_event_lists = segments  # 保留完整事件列表供后续使用
    for i, seg in enumerate(segments):
        chars = set()
        npcs = set()
        locations = set()
        types = set()
        dialogue_turns = 0
        first_line = ""
        for e in seg:
            c = e.get("character", "")
            if c.startswith("npc:"):
                npcs.add(c)
            elif c:
                chars.add(c)
            loc = e.get("location", "")
            if loc:
                locations.add(loc)
            types.add(e.get("type", ""))
            if e.get("type") in ("speak", "npc_speak") and not first_line:
                first_line = e.get("content", "")[:60]
            if e.get("type") in ("speak", "npc_speak"):
                dialogue_turns += 1

        summaries.append({
            "index": i,
            "time_start": seg[0]["ts"],
            "time_end": seg[-1]["ts"],
            "event_count": len(seg),
            "characters": sorted(chars),
            "npcs": sorted(npcs),
            "locations": sorted(locations),
            "types": sorted(types),
            "dialogue_turns": dialogue_turns,
            "preview": first_line,
        })

    return summaries, _segment_event_lists


def step_screenplay(events: list[dict]) -> dict | None:
    """两步编剧：代码分段摘要 → LLM 选题 → LLM 写剧本"""
    skill = _load_skill("screenplay")
    dedup = _get_dedup_context()

    # 过滤性行为描写事件
    if INTIMATE_FILTER:
        filtered = _filter_explicit(events)
        if len(filtered) < len(events):
            logger.info("过滤掉 %d 条性行为描写事件", len(events) - len(filtered))
        events = filtered

    if not events:
        logger.info("无事件，跳过")
        return None

    # ── 第一步：代码分段 ──
    seg_summaries, seg_event_lists = _segment_events(events)
    logger.info("代码分段: %d 个时间段", len(seg_summaries))

    # ── 第二步：LLM 选题（只看摘要）──
    select_system = "你是拍摄系统的选题编辑。从时间段摘要中选出最适合拍成 60 秒短视频的一个时间段。\n\n直接输出 JSON，不要输出分析过程。"
    select_user = f"以下是今天的事件时间段摘要：\n{json.dumps(seg_summaries, ensure_ascii=False, indent=2)}"
    if dedup:
        select_user += f"\n\n已拍摄过的时间段（请跳过）：\n{dedup}"
    if INTIMATE_FILTER:
        select_user += f"\n{INTIMATE_FILTER_INSTRUCTION}"
    select_user += '\n\n选一个最有故事性的时间段，输出 JSON：{"selected_index": 数字, "reason": "选择理由"}'

    logger.info("选题: %d 个时间段摘要, calling LLM...", len(seg_summaries))
    t0 = time.time()
    select_text = _call_llm(select_system, select_user)
    duration = int((time.time() - t0) * 1000)
    selection = _extract_json(select_text)

    if not selection or "selected_index" not in selection:
        _report_span("select", "llm_aux", input_text=select_user[:2000], error="selection failed", duration_ms=duration)
        logger.error("选题失败: %s", select_text[:300])
        return None

    idx = selection["selected_index"]
    if idx < 0 or idx >= len(seg_summaries):
        logger.error("选题返回的 index %d 超出范围 [0, %d)", idx, len(seg_summaries))
        return None

    selected = seg_summaries[idx]
    logger.info("选题完成: 段 %d (%s ~ %s) %d 事件, 理由: %s",
                idx, selected["time_start"][11:19], selected["time_end"][11:19],
                selected["event_count"], selection.get("reason", "")[:60])
    _report_span("select", "llm_aux", input_text=select_user[:2000],
                 output_text=json.dumps(selection, ensure_ascii=False)[:1000], duration_ms=duration)

    # ── 第三步：直接用分段里的事件，LLM 写剧本 ──
    selected_events = seg_event_lists[idx]
    logger.info("写剧本: %d 条完整事件", len(selected_events))

    screenplay_system = f"你是拍摄系统的编剧。根据以下事件写一个 60 秒以内的视频剧本。\n\n直接输出 JSON，不要输出分析过程。\n\n{skill}"
    if INTIMATE_FILTER:
        screenplay_system += f"\n{INTIMATE_FILTER_INSTRUCTION}"

    screenplay_user = f"以下是选中时间段的完整事件：\n{json.dumps(selected_events, ensure_ascii=False)}"

    t0 = time.time()
    result_text = _call_llm(screenplay_system, screenplay_user)
    duration = int((time.time() - t0) * 1000)
    result = _extract_json(result_text)

    if not result:
        _report_span("screenplay", "llm_aux", input_text=screenplay_user[:2000], error="JSON parse failed", duration_ms=duration)
        # 尝试详细诊断解析失败原因
        import traceback
        m = re.search(r"```(?:json)?\s*\n([\s\S]+?)\n```", result_text)
        if m:
            try:
                json.loads(m.group(1))
            except json.JSONDecodeError as e:
                logger.error("JSON 解析错误（代码块）: %s, pos=%d, around: %s", e.msg, e.pos, m.group(1)[max(0,e.pos-50):e.pos+50])
        m2 = re.search(r"\{[\s\S]+\}", result_text)
        if m2:
            try:
                json.loads(m2.group())
            except json.JSONDecodeError as e:
                logger.error("JSON 解析错误（裸JSON）: %s, pos=%d, around: %s", e.msg, e.pos, m2.group()[max(0,e.pos-50):e.pos+50])
        logger.error("编剧返回无法解析（前500字符）: %s", result_text[:500])
        return None

    if result.get("skip"):
        _report_span("screenplay", "llm_aux", output_text=f"skip: {result.get('reason','')}", duration_ms=duration)
        logger.info("编剧: skip — %s", result.get("reason", ""))
        return None

    _report_span("screenplay", "llm_aux", input_text=screenplay_user[:2000],
                 output_text=json.dumps(result, ensure_ascii=False)[:5000], duration_ms=duration,
                 metadata={"logline": result.get("logline",""), "segments": len(result.get("segments",[]))})
    logger.info("编剧完成: %s | %d segments", result.get("logline", "")[:80], len(result.get("segments", [])))
    return result


# ── 第二步：摄影设计 ──────────────────────────────────────────────

def step_cinematography(screenplay: dict, wardrobe_summary: dict, npc_visuals: dict | None = None) -> dict | None:
    """调 LLM 按剧本设计分镜，输出 SegmentPlan"""
    cinematography_skill = _load_skill("cinematography")
    kling_skill = _load_skill("kling-constraints")

    system = f"你是拍摄系统的摄影师。根据编剧写好的剧本，设计每段的具体分镜。\n\n直接输出 JSON，不要输出分析过程或思考内容。\n\n{cinematography_skill}\n\n{kling_skill}"

    user = f"""编剧剧本：
{json.dumps(screenplay, ensure_ascii=False, indent=2)}

各角色衣橱（用于匹配 outfit_item_id）:
{json.dumps(wardrobe_summary, ensure_ascii=False, indent=2)}"""

    if npc_visuals:
        user += f"""

NPC 外貌信息（NPC 没有 element，外貌必须写在 shot_prompt 中）:
{json.dumps(npc_visuals, ensure_ascii=False, indent=2)}"""

    user += "\n\n按剧本设计每段分镜，输出 SegmentPlan JSON。"

    logger.info("摄影设计: calling LLM...")
    t0 = time.time()
    result_text = _call_llm(system, user)
    duration = int((time.time() - t0) * 1000)
    result = _extract_json(result_text)

    # 容错：Opus 有时会把 segments 包在 story 或其他 key 里
    if result and "segments" not in result:
        for v in result.values():
            if isinstance(v, dict) and "segments" in v:
                result = v
                break
            elif isinstance(v, list) and v and isinstance(v[0], dict) and "shots" in v[0]:
                result = {"segments": v}
                break

    if not result or "segments" not in result:
        _report_span("cinematography", "llm_aux", input_text=user[:2000], error="invalid response", duration_ms=duration)
        logger.error("摄影设计返回无效: %s", result_text[:500])
        return None

    _report_span("cinematography", "llm_aux", input_text=user[:2000], output_text=json.dumps(result, ensure_ascii=False)[:5000], duration_ms=duration,
                 metadata={"segments": len(result["segments"])})
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

            # 视角约束（NPC 名字也要列入允许出现的角色）
            perspective = seg.get("perspective", "first_person")
            npc_names = [c.get("display_name", c.get("character_id", "")) for c in seg.get("characters", []) if c.get("is_npc")]
            npc_note = f"以及{' '.join(npc_names)}" if npc_names else ""
            lines.append("")
            if perspective == "first_person":
                lines.append(f"第一人称视角，镜头是拍摄者的眼睛。角色看向镜头表示与拍摄者对话。画面中只出现上述角色{npc_note}，不出现其他任何人。")
            else:
                lines.append(f"电影镜头视角，角色之间自然互动，不看镜头。画面中只出现上述角色{npc_note}，不出现其他任何人。")

            prompt = "\n".join(lines)

            # 收集角色参考图（定妆照）→ 上传 OSS 拿 URL
            # 同时记录角色名↔图片序号的对应关系，用于在 prompt 中声明身份
            ref_images = []
            ref_char_names = []  # 与 ref_images 一一对应
            for char in seg.get("characters", []):
                char_id = char.get("character_id", "")
                item_id = char.get("outfit_item_id", "")
                display_name = char.get("display_name", char_id)
                if char_id and item_id:
                    base_img = os.path.join(OPENFANG_HOME, "agents", char_id, "wardrobe", item_id, "base.png")
                    if os.path.exists(base_img):
                        try:
                            url = upload_to_oss(base_img, kind="images")
                            ref_images.append(url)
                            ref_char_names.append(display_name)
                            logger.info("  角色参考图: %s/%s → %s", char_id, item_id, url)
                        except Exception as e:
                            logger.warning("  角色参考图上传失败: %s", e)

            # 身份声明前缀：告诉视频模型每张参考图对应哪个角色
            if len(ref_char_names) > 1:
                identity_lines = [f"图片 {i+1} 是{name}" for i, name in enumerate(ref_char_names)]
                prompt = "\n".join(identity_lines) + "\n\n" + prompt
            elif len(ref_char_names) == 1:
                prompt = f"参考图是{ref_char_names[0]}\n\n" + prompt

            body = {
                "provider": provider,
                "prompt": prompt,
                "duration": int(seg.get("duration_seconds", 10)),
                "aspect_ratio": seg.get("aspect_ratio", "16:9"),
            }
            if ref_images:
                body["reference_images"] = ref_images

            _report_span(f"segment_{seg.get('segment_index',0)}_submit", "custom",
                         input_text=prompt, metadata={"provider": provider, "duration": body["duration"],
                                                       "ref_images": len(ref_images)})
            logger.info("提交 segment %d via %s (%ds)\nprompt:\n%s", seg.get("segment_index", 0), provider, body["duration"], prompt)
            try:
                resp = httpx.post(f"{MEDIA_SERVICE_URL}/video/generate", json=body, timeout=30)
                resp.raise_for_status()
                task_info = resp.json()
                tasks.append({"seg": seg, "taskId": task_info["taskId"], "provider": task_info["provider"], "prompt": prompt})
            except Exception as e:
                _report_span(f"segment_{seg.get('segment_index',0)}_submit_error", "custom", error=str(e))
                logger.warning("segment %d 提交失败（跳过）: %s", seg.get("segment_index", 0), e)

        # 轮询所有任务
        video_paths = []
        prompts = []
        for task in tasks:
            task_id = task["taskId"]
            task_provider = task["provider"]
            logger.info("轮询 segment %d task=%s", task["seg"].get("segment_index", 0), task_id)
            poll_start = time.time()

            for _ in range(120):  # 最多 20 分钟
                time.sleep(10)
                resp = httpx.get(f"{MEDIA_SERVICE_URL}/task/{task_provider}/{task_id}", timeout=30)
                result = resp.json()

                if result["status"] == "completed":
                    video_url = result["result"]["url"]
                    dest = os.path.join(tmp_dir, f"segment_{task['seg'].get('segment_index', 0)}.mp4")
                    dl_resp = httpx.get(video_url, timeout=120)
                    with open(dest, "wb") as f:
                        f.write(dl_resp.content)
                    video_paths.append(dest)
                    prompts.append(task["prompt"])
                    _report_span(f"segment_{task['seg'].get('segment_index',0)}_done", "custom",
                                 output_text=video_url[:200], duration_ms=int((time.time() - poll_start) * 1000),
                                 metadata={"provider": task_provider, "task_id": task_id})
                    logger.info("segment %d 完成: %s", task["seg"].get("segment_index", 0), dest)
                    break
                elif result["status"] == "failed":
                    err_msg = result.get("error", "generation failed")
                    # fallback 到 kling
                    if task_provider != "kling":
                        _report_span(f"segment_{task['seg'].get('segment_index',0)}_fallback", "custom",
                                     input_text=f"原 provider {task_provider} 失败: {err_msg}",
                                     output_text="fallback 到 kling",
                                     metadata={"original_provider": task_provider, "original_task_id": task_id, "error": err_msg})
                        logger.warning("segment %d %s 失败，fallback 到 kling: %s", task["seg"].get("segment_index", 0), task_provider, err_msg)
                        try:
                            fb_body = {**body, "provider": "kling"}
                            fb_resp = httpx.post(f"{MEDIA_SERVICE_URL}/video/generate", json=fb_body, timeout=30)
                            fb_resp.raise_for_status()
                            fb_info = fb_resp.json()
                            task_id = fb_info["taskId"]
                            task_provider = fb_info["provider"]
                            task["taskId"] = task_id
                            task["provider"] = task_provider
                            poll_start = time.time()
                            logger.info("  kling fallback 已提交: task=%s", task_id)
                            continue  # 继续轮询 kling 的任务
                        except Exception as fb_e:
                            logger.warning("  kling fallback 也失败: %s", fb_e)

                    _report_span(f"segment_{task['seg'].get('segment_index',0)}_failed", "custom",
                                 error=err_msg,
                                 duration_ms=int((time.time() - poll_start) * 1000),
                                 metadata={"provider": task_provider, "task_id": task_id})
                    logger.warning("segment %d 失败（跳过）: %s", task["seg"].get("segment_index", 0), err_msg)
                    break
            else:
                _report_span(f"segment_{task['seg'].get('segment_index',0)}_timeout", "custom",
                             error="polling timeout", metadata={"provider": task_provider, "task_id": task_id})
                logger.warning("segment %d 轮询超时（跳过）", task["seg"].get("segment_index", 0))

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

    _start_trace()
    logger.info("=== 开始拍摄 (since=%s) trace=%s ===", since, _current_trace_id)

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

    # 读 NPC 外貌（如果剧本中有 NPC）
    npc_visuals = {}
    screenplay_npcs = screenplay.get("npcs", [])
    if screenplay_npcs:
        npc_ids = [n["npc_id"] if isinstance(n, dict) else n for n in screenplay_npcs]
        npc_visuals = read_npc_visuals(npc_ids)
        logger.info("读取 %d 个 NPC 外貌: %s", len(npc_visuals), list(npc_visuals.keys()))

    # 第二步：摄影设计
    segment_plan = step_cinematography(screenplay, wardrobe_summary, npc_visuals=npc_visuals or None)
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

        _end_trace("completed")
        return result
    except Exception:
        logger.exception("pipeline 执行失败")
        _end_trace("error")
        return None
    finally:
        cleanup_oss_uploads()


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
