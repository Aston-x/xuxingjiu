"""InvokeAI 适配器 —— **只支持一条可验证路径**，其余组合明确自报 unsupported。

为什么这么保守：InvokeAI 的 REST 接口在 3.x / 4.x / 5.x 之间改动很大
（`/api/v1/...` 的 graph 格式、队列端点、甚至字段名都换过）。我没有可以实测的环境，
与其写一堆"看起来能跑"的分支，不如：

  · 只走 `/api/v1/queue/default/enqueue_batch` → `/api/v1/queue/default/b/{id}` →
    `/api/v1/images/i/{name}/full` 这一条 4.x 风格的最小路径；
  · 任何不满足前提的情况（比如没给 graph 模板、或服务端返回了别的形状）
    **直接报错**，并由 `probe()` 自报 `unsupported: True`；
  · `doctor.py` 会把这个后端标成「未实测」，提示用户以官方文档为准。

宁可报"不支持"，也不要假装支持然后画出一张错的图。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import (
    REASON_BAD_CONFIG,
    REASON_EMPTY_IMAGE,
    REASON_TIMEOUT,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)

WORKFLOW_DIR = pathlib.Path(__file__).resolve().parent.parent / "workflows"


@register_image_provider("invokeai")
class InvokeAIProvider(ImageProvider):
    name = "invokeai"
    display_name = "InvokeAI（本地，未实测）"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=True, size_step=8, negative=True, hires=False,
            returns="b64", formats=("png",), seed=True, multi_hop=True,
            note="未实测：只支持「固定 graph 模板 + 队列两跳」这一条路径，其余情况会明确报不支持",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（InvokeAI 默认 http://127.0.0.1:9090）"
        wf = str(ep.params.get("graph") or ep.params.get("workflow") or "").strip()
        if not wf:
            return False, ("没给工作流模板：InvokeAI 必须以 endpoints[].params.graph "
                           "指定一份 graph JSON（见 imagegen/workflows/README.md）")
        return True, ""

    def _graph(self, ep: ImageEndpoint) -> dict:
        name = str(ep.params.get("graph") or ep.params.get("workflow") or "")
        p = WORKFLOW_DIR / name
        if not p.is_file():
            raise ValueError(f"{REASON_BAD_CONFIG}: 找不到 graph 文件 {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        graph = self._graph(ep)
        # 极简注入：只认 graph 里带 `_xuxingjiu` 标记的字段
        # （不猜 InvokeAI 的节点结构 —— 猜错就是画错，不如要求模板自带占位）
        raw = json.dumps(graph, ensure_ascii=False)
        raw = (raw.replace("__PROMPT__", json.dumps(req.prompt, ensure_ascii=False)[1:-1])
                  .replace("__NEGATIVE__", json.dumps(req.negative, ensure_ascii=False)[1:-1])
                  .replace("__WIDTH__", str(int(req.width)))
                  .replace("__HEIGHT__", str(int(req.height))))
        try:
            graph = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"{REASON_BAD_CONFIG}: graph 注入后不是合法 JSON（{exc}）") from exc

        root = ep.base_url.rstrip("/")
        resp = await http.send(ep, Request(
            method="POST", url=root + "/api/v1/queue/default/enqueue_batch",
            headers={"Content-Type": "application/json"},
            json_body={"batch": {"graph": graph, "runs": 1}}), timeout=ep.timeout)
        payload = resp.json() or {}
        item_ids = payload.get("item_ids") or ([payload["item_id"]] if payload.get("item_id") else [])
        if not item_ids:
            raise ValueError(f"InvokeAI 没返回队列 id：{str(payload)[:200]}")
        item_id = str(item_ids[0])

        deadline = time.time() + float(ep.timeout or 180)
        image_name = ""
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            h = await http.send(ep, Request(
                method="GET", url=f"{root}/api/v1/queue/default/b/{item_id}"), timeout=15)
            item = h.json() or {}
            status = str(item.get("status") or "")
            if status in ("failed", "canceled"):
                raise ValueError(f"InvokeAI 出图失败（{status}）：{str(item)[:200]}")
            results = item.get("result") or {}
            img = results.get("image") or {}
            if status == "completed" and img.get("image_name"):
                image_name = str(img["image_name"])
                break
        if not image_name:
            raise TimeoutError(f"{REASON_TIMEOUT}: 等 InvokeAI 出图超时（{ep.timeout}s）")

        got = await http.send(ep, Request(
            method="GET", url=f"{root}/api/v1/images/i/{image_name}/full"), timeout=60)
        data = got.content or b""
        if not data:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: InvokeAI 取回的图片是空的")
        return ImageResult(data=data, endpoint_id=ep.id, meta={"image_name": image_name})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        """**自报 unsupported** —— 这个适配器没有实测环境，不要让人误以为它能用。

        只做一件事：确认端口上确实有个 HTTP 服务在响应（证明 base_url 没写错）。
        """
        root = (ep.base_url or "").rstrip("/")
        if not root:
            return {"unsupported": True, "error": "没配 base_url"}
        try:
            await http.get(ep, root + "/api/v1/app/version", timeout=6)
        except Exception as exc:  # noqa: BLE001
            return {"unsupported": True, "error": str(exc)[:120]}
        return {"unsupported": True,
                "note": "InvokeAI 接口版本差异大，本适配器未实测；请以官方文档为准"}
