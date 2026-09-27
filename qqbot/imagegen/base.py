"""生图 Provider 的抽象基类与注册钩子。

与 `providers/base.py` 同构，但**独立注册表** —— 生图后端和对话模型是两回事，
名字空间必须分开（否则一个叫 `openai` 的对话适配器和一个叫 `openai` 的生图适配器会打架）。

一个 ImageProvider 只做一件事：**拿到 ImageEndpoint 和 GenRequest，把图弄回来**
（多跳、轮询、下载都归它自己管）。不碰配额、不碰落盘、不碰提示词编排。
"""

from __future__ import annotations

import abc
from typing import Any

from providers.types import Request  # noqa: F401  （子类构造请求时用）
from .types import ImageCapability, ImageEndpoint, ImageResult, GenRequest


class ImageProvider(abc.ABC):
    """生图后端适配器。"""

    name: str = ""
    display_name: str = ""

    # ── 必须实现 ──

    @abc.abstractmethod
    async def run(self, ep: ImageEndpoint, req: GenRequest, http: Any) -> ImageResult:
        """出一张图。

        `http` 是 `providers.transport.Transport` 实例：用它发请求才能拿到
        「本机绕过 HTTP_PROXY」和「4xx 原文进日志」这两个既有保障。
        多跳后端（ComfyUI 的 提交→轮询→取图、云端异步任务）在这里自己循环，
        但**轮询必须带上限**，别写成死循环。
        """

    # ── 可选覆盖 ──

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability()

    def capability(self, ep: ImageEndpoint) -> ImageCapability:
        """端点显式声明的能力优先于适配器默认。"""
        return ep.image or self.default_capabilities()

    @staticmethod
    def bearer_headers(ep: ImageEndpoint) -> dict:
        return {"Authorization": f"Bearer {ep.auth()}", "Content-Type": "application/json"}

    async def download(self, http, ep: ImageEndpoint, url: str, *, timeout: float = 60.0) -> bytes:
        """云端返回的是图片 URL 时，把它取回来。

        **带大小上限**：一张 4K 图 base64 解码 + 写盘会瞬时吃两倍内存，
        没有上限时内存小的机器会直接 OOM，而不是"这张画失败"。
        """
        limit = int(self.capability(ep).max_bytes or 0)
        resp = await http.get(ep, url, timeout=timeout)
        resp.raise_for_status()
        data = resp.content or b""
        if limit and len(data) > limit:
            raise ValueError(f"远端图片超过大小上限（{len(data)} > {limit}）")
        return data

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        """纯配置判定：这个端点现在能不能用（**不发请求**）。

        云端后端要在这里检查有没有密钥 —— 因为云端不做真探针（可能计费），
        「能不能用」只能靠配置 + 负缓存回答。
        """
        if not (ep.base_url or "").strip() and self.name not in ("sd_webui", "comfyui"):
            return False, "没配 base_url"
        return True, ""

    async def probe(self, ep: ImageEndpoint, http: Any, *, deep: bool = False) -> dict:
        """轻量探针（廉价 GET）。不支持的返回 {"unsupported": True}。

        `deep=True` 才允许做「真的试画一张」这种昂贵操作，默认不做。
        """
        return {"unsupported": True}

    def prepare(self, ep: ImageEndpoint, req: GenRequest) -> GenRequest:
        """出图前的最后调整（尺寸吸附、超长提示词截断、不支持负词时丢弃）。"""
        cap = self.capability(ep)
        w, h = cap.fit(req.width, req.height)
        notes: list[str] = []
        prompt = req.prompt
        if cap.max_prompt_chars and len(prompt) > cap.max_prompt_chars:
            prompt = prompt[: cap.max_prompt_chars]
            notes.append(f"提示词超长，已截到 {cap.max_prompt_chars} 字符")
        negative = req.negative
        if not cap.negative and negative:
            negative = ""
            notes.append("该后端不支持负面提示词，已忽略")
        params = dict(req.params or {})
        if notes:
            params["_note"] = "；".join(notes)
        return GenRequest(prompt=prompt, negative=negative, width=w, height=h,
                          params=params, purpose=req.purpose, intent=req.intent,
                          mode=req.mode)


# ── 注册钩子（独立于 chat 的 _HOOKS） ──────────────────────────────────

_HOOKS: list[tuple[str, type[ImageProvider], int]] = []


def register_image_provider(name: str, *, priority: int = 0):
    """登记一个生图适配器。用法与 `providers.base.register_provider` 一致。"""
    def deco(cls: type[ImageProvider]) -> type[ImageProvider]:
        if not issubclass(cls, ImageProvider):
            raise TypeError(f"{cls!r} 不是 ImageProvider 子类")
        cls.name = cls.name or name
        _HOOKS.append((name, cls, priority))
        return cls
    return deco


def registered_image_hooks() -> list[tuple[str, type[ImageProvider], int]]:
    return list(_HOOKS)


def clear_image_hooks() -> None:
    """只给测试用。"""
    _HOOKS.clear()
