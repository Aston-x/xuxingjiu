"""Provider 层的数据类型 —— 纯数据，无 IO、不依赖 bot。

这一层的存在意义：把「哪家模型该怎么调」从 `bot.py` 里抽出来，
让新增一家厂商变成「加一个适配器」而不是「在 9000 行的文件里找分支」。

依赖方向是单向的：`types` ← `base` ← `adapters` / `declarative` ← `registry` ← `router`。
**本包任何模块都不许 import bot**（否则 test_providers.py 就没法脱离 bot 单独跑）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

# ── 点路径工具（usage 字段映射、max_tokens 嵌套写回都靠它） ──────────────


def dget(obj: Any, path: str, default: Any = None) -> Any:
    """按点路径取值，支持下标：`usage.prompt_tokens_details.cached_tokens`、`a.b[0].c`。

    任何一段取不到（缺键 / 类型不对 / 越界）都返回 default，**不抛异常** ——
    各家返回的 usage 字段参差不齐，这里必须宽容。
    """
    if not path:
        return default
    node = obj
    for raw in _split_path(path):
        if node is None:
            return default
        if isinstance(raw, int):
            if not isinstance(node, (list, tuple)) or not -len(node) <= raw < len(node):
                return default
            node = node[raw]
        else:
            if not isinstance(node, dict) or raw not in node:
                return default
            node = node[raw]
    return node


def dset(obj: dict, path: str, value: Any) -> None:
    """按点路径写值，中间层不存在就建 dict。

    Ollama 的 `options.num_predict` 就是靠它写进去的 —— 手工拼嵌套 dict 太容易写错。
    """
    if not path:
        return
    parts = _split_path(path)
    node = obj
    for raw in parts[:-1]:
        if isinstance(raw, int):
            raise ValueError(f"点路径写回不支持数组下标：{path}")
        nxt = node.get(raw)
        if not isinstance(nxt, dict):
            nxt = {}
            node[raw] = nxt
        node = nxt
    last = parts[-1]
    if isinstance(last, int):
        raise ValueError(f"点路径写回不支持数组下标：{path}")
    node[last] = value


def _split_path(path: str) -> list[Any]:
    """`$.a.b[0].c` → ['a', 'b', 0, 'c']；也接受不带 `$.` 前缀的写法。"""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    if p.startswith("."):
        p = p[1:]
    out: list[Any] = []
    buf = ""
    i = 0
    while i < len(p):
        ch = p[i]
        if ch == ".":
            if buf:
                out.append(buf)
                buf = ""
        elif ch == "[":
            if buf:
                out.append(buf)
                buf = ""
            j = p.find("]", i)
            if j < 0:
                raise ValueError(f"路径方括号没闭合：{path}")
            inner = p[i + 1:j].strip().strip("'\"")
            out.append(int(inner) if inner.lstrip("-").isdigit() else inner)
            i = j
        else:
            buf += ch
        i += 1
    if buf:
        out.append(buf)
    return out


# ── 能力声明 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UsageKeys:
    """各家 usage 字段名不一样，这里做归一化。

    留空的字段一律按 0 处理（比如多数厂商没有缓存计费字段）。
    路径支持点路径与 `$` 前缀：`usage.prompt_tokens`、`$.usage.prompt_tokens_details.cached_tokens`。
    """

    prompt: str = "usage.prompt_tokens"
    completion: str = "usage.completion_tokens"
    cache_hit: str = ""
    cache_miss: str = ""

    @classmethod
    def from_dict(cls, raw: dict | None) -> "UsageKeys":
        if not raw:
            return cls()
        known = set(cls.__dataclass_fields__)
        kw = {k: str(v) for k, v in raw.items() if k in known and v is not None}
        # 允许只写后半个路径（如 "prompt_tokens"），统一补上 usage. 前缀
        for k, v in list(kw.items()):
            if v and not v.startswith(("$", "usage", "usageMetadata")):
                kw[k] = "usage." + v
        return cls(**kw)


@dataclass(frozen=True)
class Capability:
    """一个端点「会什么、不会什么」。

    重构前这些判断散落在 `_fit_vision` / `max_side` / `_post` 里，写死了 local/cloud 两档。
    现在收进这里，新增一家厂商只要声明能力，不改调用方。
    """

    # 识图
    vision: bool = False
    vision_input: str = "none"              # none | url | base64 | both
    vision_block_style: str = "openai"      # openai | anthropic | gemini

    # 推理 / 工具 / 流式
    thinking: bool = False
    thinking_param: dict = field(default_factory=dict)   # 只有该家要追加的 body 字段
    tool_calling: bool = False
    streaming: bool = True

    # 生成参数
    max_tokens_key: str = "max_tokens"      # 支持点路径，如 ollama 的 options.num_predict
    max_tokens_limit: int | None = None     # 截断翻倍重试时的硬上限
    temperature_range: tuple[float, float] = (0.0, 2.0)

    # 报文结构
    system_in_messages: bool = True         # Anthropic 为 False（system 提到顶层）
    usage: UsageKeys = field(default_factory=UsageKeys)

    # 行为
    supports_truncation_retry: bool = True
    max_image_side_default: int = 1568      # 这个端点收图时最长边默认给多少

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Capability":
        """从配置 / 清单的 dict 构造。未知键忽略，字段缺失用默认值。"""
        if not raw:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kw: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        usage = kw.pop("usage", None)
        if isinstance(usage, dict):
            kw["usage"] = UsageKeys.from_dict(usage)
        if isinstance(kw.get("temperature_range"), (list, tuple)):
            kw["temperature_range"] = tuple(kw["temperature_range"])  # type: ignore[assignment]
        if isinstance(kw.get("thinking_param"), dict):
            kw["thinking_param"] = dict(kw["thinking_param"])
        return cls(**kw)

    def clamped_temperature(self, value: float) -> float:
        """把温度夹进该家允许的范围。

        给 Kimi（0~1）传 1.5 会被服务端 400 顶回来，而调用方根本不知道自己越界了 ——
        所以这里静默夹紧，比透传报错友好得多。
        """
        lo, hi = self.temperature_range
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value


# ── 端点 ──────────────────────────────────────────────────────────────


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]")


@dataclass
class Endpoint:
    """一个可调用的模型端点。旧的 `config.local` / `config.cloud` 各映射成一个。"""

    id: str
    provider: str                            # 适配器注册名：openai / anthropic / ollama / ...
    base_url: str
    model: str
    api_key: str = ""
    api_key_env: str = ""                    # 优先级高于 api_key
    enabled: bool = True
    require_enabled: bool = True             # False = 忽略 enabled（见 registry 里的旧配置迁移）
    tier: str = "cloud"                      # local | cloud —— 决定代理策略与失败原因字面量
    concurrency: int = 0                     # 0 = 不限制
    timeout: float = 90.0
    max_tokens: int = 1024
    vision: bool | None = None               # 显式覆盖能力声明里的 vision
    deployment: str = ""                     # Azure 专用
    api_version: str = ""                    # Azure 专用
    params: dict = field(default_factory=dict)     # 额外 body 字段（如 DeepSeek 的 thinking）
    headers: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    trust_env: bool | None = None            # None = 按 is_loopback 自动判定
    capabilities: Capability | None = None   # 覆盖适配器默认能力
    image_max_side: int | None = None        # 这个端点收图时的最长边；None = 用 vision.max_side_* 兜底
    display_name: str = ""

    @property
    def is_loopback(self) -> bool:
        """是不是本机地址。决定要不要绕过 HTTP_PROXY（见 transport）。"""
        url = (self.base_url or "").lower()
        return any(h in url for h in LOOPBACK_HOSTS)

    @property
    def effective_trust_env(self) -> bool:
        """本机端点强制 `trust_env=False`，否则环境变量里的 HTTP_PROXY 会把 127.0.0.1 打死。"""
        if self.trust_env is not None:
            return self.trust_env
        return not self.is_loopback

    def auth(self) -> str:
        """取实际密钥：环境变量优先，其次配置文件里的明文值。"""
        if self.api_key_env:
            env = os.environ.get(self.api_key_env)
            if env:
                return env
        return self.api_key or ""

    def has_key(self) -> bool:
        return bool(self.auth())

    @classmethod
    def from_dict(cls, raw: dict, *, index: int = 0) -> "Endpoint":
        known = set(cls.__dataclass_fields__)
        kw: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        caps = kw.pop("capabilities", None)
        ep = cls(**kw)
        if isinstance(caps, dict):
            ep.capabilities = Capability.from_dict(caps)
            # 清单/配置里显式写了最长边就认它；没写就别动，交给 vision.max_side_* 兜底
            if "max_image_side_default" in caps:
                ep.image_max_side = int(caps["max_image_side_default"])
        if not ep.id:
            ep.id = f"{ep.provider or 'endpoint'}-{index + 1}"
        return ep

    def with_capability(self, cap: Capability) -> "Endpoint":
        return replace(self, capabilities=cap)


# ── 请求 / 响应 ───────────────────────────────────────────────────────


@dataclass
class Request:
    """适配器产出、transport 消费的与协议无关的请求描述。"""

    method: str
    url: str
    headers: dict = field(default_factory=dict)
    json_body: dict | None = None
    params: dict | None = None


@dataclass
class ChatResult:
    text: str
    usage: dict = field(default_factory=dict)     # 已归一化：in / out / hit / miss
    endpoint_id: str = ""
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)
