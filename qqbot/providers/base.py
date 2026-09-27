"""Provider 抽象基类与注册钩子。

一个 Provider 只做两件事：
  1. `build_request()` —— 把统一的 messages 编成 HTTP 请求；
  2. `parse_response()` —— 把响应解成 ChatResult。
它**不碰**配置、不碰选路、不碰重试 —— 那些是 Registry / Router 的事。
"""

from __future__ import annotations

import abc
from typing import Any

from .types import Capability, ChatResult, Endpoint, Request

# 内置协议族。新增一个协议就在 adapters/ 下加模块并在这里登记名字。
PROTOCOLS = (
    "openai",      # OpenAI /chat/completions —— 兼容面最广，DeepSeek/Kimi/GLM/Qwen/vLLM 都用它
    "azure",       # Azure OpenAI：deployment 在路径、api-key 头、api-version 查询参
    "anthropic",   # Anthropic Messages：system 顶层、content blocks、stop_reason
    "gemini",      # Google Gemini：contents/parts/system_instruction/generationConfig
    "ollama",      # Ollama 原生 /api/chat
    "lmstudio",    # LM Studio 原生 /api/v0
    "baidu",       # 百度文心
    "xinghuo",     # 讯飞星火
    "hunyuan",     # 腾讯混元
    "declarative", # 由声明式清单驱动的通用适配器
)

# 图片降级占位措辞 —— **这两句话是反幻觉的关键，改动请同步改 test_providers.py**。
# 后端能看图、只是这条通道传不了图时说前者；后端本身看不了图时说后者。
# 说反了她会以为"后端看不见"，于是把刚刚通过识图拿到的描述也一起否认掉。
VISION_PLACEHOLDER_NO_CHANNEL = "（他发了张图片，这条通道传不了图，你看不到内容。别编，也别装作看见了。）"
VISION_PLACEHOLDER_BLIND = "（他发了张图片，你看不到图里的内容。别编，也别装作看见了。）"


class Provider(abc.ABC):
    """协议适配器基类。"""

    name: str = ""
    display_name: str = ""

    # ── 必须实现 ──

    @abc.abstractmethod
    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        """把 messages 编成 HTTP 请求。extra 是端点级 params（不含协议固定字段）。"""

    @abc.abstractmethod
    def parse_response(self, ep: Endpoint, data: dict) -> ChatResult:
        """把响应体解成 ChatResult（含 usage 归一化）。"""

    # ── 可选覆盖 ──

    def default_capabilities(self) -> Capability:
        return Capability()

    def capability(self, ep: Endpoint) -> Capability:
        """该端点**实际生效**的能力：端点显式声明 > 适配器默认。

        `endpoint.vision` 是单独的一个开关（它比整体 capabilities 更常被改），
        所以这里额外套一层覆盖。
        """
        base = ep.capabilities or self.default_capabilities()
        if ep.vision is None:
            return base
        from dataclasses import replace
        return replace(base, vision=bool(ep.vision))

    def adapt_messages(self, messages: list, ep: Endpoint) -> list:
        """消息体转换。OpenAI 系原样返回；Anthropic / Gemini 必须覆盖。"""
        return messages

    def prepare_messages(self, messages: list, ep: Endpoint, cap: Capability,
                         *, backend_can_see: bool) -> list:
        """完整预处理：先协议转换，再按识图能力决定要不要把图片换成文字占位。

        不支持视图的端点收到 image_url 会直接把整个请求 400 掉，而且图片留在上下文里
        会让**之后连续好多轮**都失败 —— 所以必须在这一层拦掉。
        """
        msgs = self.adapt_messages(messages, ep)
        if cap.vision and cap.vision_input != "none":
            return msgs
        return self.vision_placeholder(msgs, ep, backend_can_see=backend_can_see)

    @staticmethod
    def vision_placeholder(messages: list, ep: Endpoint, *, backend_can_see: bool) -> list:
        text = VISION_PLACEHOLDER_NO_CHANNEL if backend_can_see else VISION_PLACEHOLDER_BLIND
        out: list = []
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, list):
                out.append(m)
                continue
            parts: list = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    parts.append({"type": "text", "text": text})
                else:
                    parts.append(p)
            out.append({**m, "content": parts})
        return out

    def parse_error(self, resp: Any) -> str:
        """出错时把服务端原话记下来 —— 只留个 HTTPStatusError 是查不出原因的。"""
        try:
            return (resp.text or "")[:400]
        except Exception:  # noqa: BLE001
            return ""

    def needs_truncation_retry(self, result: ChatResult) -> bool:
        """话没说完就被 max_tokens 卡断：额度翻倍再要一次。

        OpenAI 系给 `finish_reason == "length"`，Anthropic 给 `stop_reason == "max_tokens"`。
        """
        return not result.text and result.finish_reason in ("length", "max_tokens")

    async def probe(self, ep: Endpoint, http: Any) -> dict:
        """端点健康探测。不支持的返回 {"unsupported": True}。"""
        return {"unsupported": True}

    # ── 工具 ──

    @staticmethod
    def bearer_headers(ep: Endpoint) -> dict:
        return {"Authorization": f"Bearer {ep.auth()}",
                "Content-Type": "application/json"}


# ── 注册钩子 ──────────────────────────────────────────────────────────

_HOOKS: list[tuple[str, type[Provider], int]] = []


def register_provider(name: str, *, priority: int = 0):
    """把一个 Provider 类登记进内置表。

    用法：
        @register_provider("openai")
        class OpenAIProvider(Provider): ...

    priority 高的在**同名冲突**时胜出；默认 0。插件想覆盖内置同名适配器，
    需要显式给更高 priority 并在 Registry 上允许覆盖（见 registry.register）。
    """
    def deco(cls: type[Provider]) -> type[Provider]:
        if not issubclass(cls, Provider):
            raise TypeError(f"{cls!r} 不是 Provider 子类")
        cls.name = cls.name or name
        _HOOKS.append((name, cls, priority))
        return cls
    return deco


def registered_hooks() -> list[tuple[str, type[Provider], int]]:
    """给 Registry 消费；返回副本，外部改不动内部表。"""
    return list(_HOOKS)


def clear_hooks() -> None:
    """只给测试用：重置注册表。"""
    _HOOKS.clear()
