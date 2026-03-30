"""KlingAI Python 客户端 — 移植自 Video3Agent 的 kling_provider.py

只保留拍摄系统需要的功能：
- JWT 认证
- omni-video 提交（含 element_list）
- 任务轮询
不包含角色注册（element 从 OpenFang 衣橱读取）
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
import jwt

logger = logging.getLogger(__name__)


class KlingAPIError(Exception):
    def __init__(self, code: int, message: str, request_id: str = ""):
        self.code = code
        self.request_id = request_id
        super().__init__(f"KlingAI error code={code} msg={message} req={request_id}")


class KlingClient:
    """KlingAI HTTP 客户端，JWT 认证 + 请求 + 轮询"""

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        token_ttl: int = 1800,
    ) -> None:
        self._access_key = access_key or os.environ["KLING_ACCESS_KEY"]
        self._secret_key = secret_key or os.environ["KLING_SECRET_KEY"]
        self._base_url = base_url or os.environ.get("KLING_BASE_URL", "https://api.klingai.com")
        self._token_ttl = token_ttl
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)

    @property
    def _auth_token(self) -> str:
        now = time.time()
        if now >= self._token_expires_at - 60:
            headers = {"alg": "HS256", "typ": "JWT"}
            payload = {
                "iss": self._access_key,
                "exp": int(now) + self._token_ttl,
                "nbf": int(now) - 5,
            }
            self._token = jwt.encode(payload, self._secret_key, headers=headers)
            self._token_expires_at = now + self._token_ttl
        return self._token

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {self._auth_token}",
                "Content-Type": "application/json",
            },
        )
        return self._parse_response(resp)

    async def get(self, path: str) -> dict[str, Any]:
        resp = await self._http.get(
            path,
            headers={"Authorization": f"Bearer {self._auth_token}"},
        )
        return self._parse_response(resp)

    def _parse_response(self, resp: httpx.Response) -> dict[str, Any]:
        body = resp.json()
        code = body.get("code")
        if code != 0:
            raise KlingAPIError(
                code=code,
                message=body.get("message", ""),
                request_id=body.get("request_id", ""),
            )
        return body.get("data", {})

    async def close(self) -> None:
        await self._http.aclose()


async def submit_video(
    client: KlingClient,
    prompt: str,
    element_list: list[dict[str, str]],
    aspect_ratio: str = "16:9",
    duration_seconds: int = 8,
    first_frame_url: str | None = None,
    mode: str = "std",
) -> str:
    """提交 omni-video 生成任务，返回 task_id"""
    body: dict[str, Any] = {
        "model_name": "kling-v3-omni",
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "duration": str(duration_seconds),
        "sound": "on",
        "mode": mode,
    }
    if element_list:
        body["element_list"] = element_list

    if first_frame_url:
        body["image_list"] = [{"image_url": first_frame_url, "type": "first_frame"}]

    logger.info(
        "kling_submit: elements=%d prompt_len=%d duration=%ds first_frame=%s",
        len(element_list),
        len(prompt),
        duration_seconds,
        bool(first_frame_url),
    )

    data = await client.post("/v1/videos/omni-video", body)
    task_id = data["task_id"]
    logger.info("kling_submitted: task_id=%s", task_id)
    return task_id


async def poll_video(
    client: KlingClient,
    task_id: str,
    interval: float = 10.0,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """轮询视频生成任务直到完成或超时，返回完整的 task data"""
    start = time.monotonic()
    while True:
        data = await client.get(f"/v1/videos/omni-video/{task_id}")
        status = data.get("task_status", "")

        if status == "succeed":
            logger.info("kling_succeed: task_id=%s", task_id)
            return data
        if status == "failed":
            msg = data.get("task_status_msg", "unknown error")
            raise KlingAPIError(code=-1, message=f"task failed: {msg}")

        elapsed = time.monotonic() - start
        if elapsed + interval > timeout:
            raise KlingAPIError(code=-1, message=f"polling timeout after {elapsed:.0f}s")

        logger.debug("kling_polling: task_id=%s status=%s elapsed=%.0fs", task_id, status, elapsed)
        await asyncio.sleep(interval)


async def download_video(url: str, dest_path: str) -> None:
    """下载视频文件到本地路径"""
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)
    logger.info("kling_downloaded: %s -> %s", url, dest_path)
