"""通义万相（DashScope）适配器 —— **异步任务**式接口，要两跳。

  1. `POST {base}/api/v1/services/aigc/text2image/image-synthesis`
     （必须带 `X-DashScope-Async: enable`）→ `output.task_id`
  2. 轮询 `GET {base}/api/v1/tasks/{task_id}` 直到 `task_status == SUCCEEDED`
     → `output.results[0].url`，然后再下载一次

尺寸用 `1024*1024` 这种「星号」写法（不是 `x`），这是它和 OpenAI 系的差别之一。
⚠️ 未实测：接口形状按公开文档写，字段名以官方文档为准。
"""

from __future__ import annotations

import asyncio
import time

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import (
    REASON_EMPTY_IMAGE,
    REASON_TIMEOUT,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)


@register_image_provider("dashscope")
class DashScopeProvider(ImageProvider):
    name = "dashscope"
    display_name = "通义万相（DashScope）"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=False,
            presets=((1024, 1024), (1280, 720), (720, 1280), (1440, 810), (810, 1440)),
            negative=True,
            hires=False,
            returns="url",
            formats=("png", "jpg"),
            max_prompt_chars=800,
            seed=True,
            moderation=True,
            multi_hop=True,
            note="异步任务式接口；未实测，字段以官方文档为准",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（默认 https://dashscope.aliyuncs.com）"
        if not ep.has_key():
            return False, f"没配密钥（环境变量 {ep.api_key_env or 'DASHSCOPE_API_KEY'}）"
        return True, ""

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        root = ep.base_url.rstrip("/")
        headers = {**self.bearer_headers(ep), "X-DashScope-Async": "enable"}
        body: dict = {
            "model": ep.model or "wanx2.1-t2i-turbo",
            "input": {"prompt": req.prompt},
            "parameters": {"n": 1, "size": f"{int(req.width)}*{int(req.height)}"},
        }
        if req.negative:
            body["input"]["negative_prompt"] = req.negative
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                body["parameters"][k] = v

        resp = await http.send(ep, Request(
            method="POST",
            url=root + "/api/v1/services/aigc/text2image/image-synthesis",
            headers=headers, json_body=body), timeout=ep.timeout)
        payload = resp.json() or {}
        task_id = str((payload.get("output") or {}).get("task_id") or "")
        if not task_id:
            raise ValueError(f"DashScope 没返回 task_id：{str(payload)[:200]}")

        deadline = time.time() + float(ep.timeout or 180)
        link = ""
        while time.time() < deadline:
            await asyncio.sleep(2.0)
            h = await http.send(ep, Request(
                method="GET", url=f"{root}/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {ep.auth()}"}), timeout=15)
            got = h.json() or {}
            out = got.get("output") or {}
            status = str(out.get("task_status") or "").upper()
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise ValueError(f"DashScope 任务失败（{status}）：{str(got)[:200]}")
            if status == "SUCCEEDED":
                results = out.get("results") or []
                link = str((results[0] or {}).get("url") or "") if results else ""
                break
        if not link:
            raise TimeoutError(f"{REASON_TIMEOUT}: 等 DashScope 出图超时（{ep.timeout}s）")
        raw = await self.download(http, ep, link)
        return ImageResult(data=raw, endpoint_id=ep.id, meta={"task_id": task_id})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        # 云端不真探针（可能计费）；只回报配置状态
        if not ep.has_key():
            return {"unsupported": True, "error": "没配密钥"}
        return {"unsupported": True, "note": "云端异步接口，不主动探测（避免计费）"}
