"""生图层的纯数据类型。

复用 `providers.types.Endpoint` 作为端点基类 —— 这样 `providers.transport.Transport`
（本机绕过代理 + 客户端缓存 + 4xx 原文日志）可以一行不改地拿来用。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from providers.types import Capability, Endpoint  # noqa: F401  （Endpoint 给子类用）


@dataclass(frozen=True)
class ImageCapability:
    """一个生图后端「会什么、不会什么」。

    `free_size=False` 的后端只能吃预设档位（云端普遍如此），
    所以 `fit_size` 必须先把尺寸吸附到合法值，否则直接 400。
    """

    free_size: bool = True                 # 尺寸能否自由指定
    size_step: int = 8                     # 本地后端要求对齐的步长
    presets: tuple[tuple[int, int], ...] = ()   # 云端允许的档位（free_size=False 时用）
    negative: bool = True                  # 支持负面提示词
    hires: bool = True                     # 支持放大/二次采样
    returns: str = "b64"                   # b64 | url
    formats: tuple[str, ...] = ("png",)
    max_prompt_chars: int = 2000
    seed: bool = True
    moderation: bool = False               # 有内容审核（提示词可能被拒）
    multi_hop: bool = False                # 需要轮询（ComfyUI / 云端异步任务）
    max_bytes: int = 32 * 1024 * 1024      # 单图上限，流式下载时防 OOM
    note: str = ""                         # 给面板/doctor 看的备注（如「未实测」）

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ImageCapability":
        if not raw:
            return cls()
        known = set(cls.__dataclass_fields__)
        kw = {k: v for k, v in raw.items() if k in known}
        if isinstance(kw.get("presets"), (list, tuple)):
            kw["presets"] = tuple(tuple(int(x) for x in p) if isinstance(p, (list, tuple)) else p
                                  for p in kw["presets"])
        if isinstance(kw.get("formats"), (list, tuple)):
            kw["formats"] = tuple(str(x) for x in kw["formats"])
        return cls(**kw)

    def fit(self, w: int, h: int) -> tuple[int, int]:
        """把宽高夹成这个后端能接受的值。

        本地（free_size=True）：按 size_step 对齐（SD 系列不对齐会报错或出怪图）。
        云端（free_size=False）：吸附到最近的预设档（OpenAI 只认 3 个尺寸，多一像素都 400）。
        """
        w = max(64, int(w))
        h = max(64, int(h))
        if self.free_size:
            step = max(1, int(self.size_step or 1))
            return max(step, (w // step) * step), max(step, (h // step) * step)
        if not self.presets:
            return w, h
        def dist(p: tuple[int, int]) -> int:
            return abs(p[0] - w) + abs(p[1] - h)
        best = min(self.presets, key=dist)
        return int(best[0]), int(best[1])


@dataclass
class ImageEndpoint(Endpoint):
    """生图端点。继承 chat 的 Endpoint，多加一个 image 能力字段。"""

    image: ImageCapability | None = None   # 端点显式声明的能力；None = 用适配器默认
    display_name: str = ""

    @classmethod
    def from_dict(cls, raw: dict, *, index: int = 0) -> "ImageEndpoint":
        known = set(cls.__dataclass_fields__)
        kw: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        kw.pop("image", None)
        # 继承自 Endpoint 的字段没有默认值，缺了会 TypeError —— 生图端点常常不写 model
        for key in ("id", "provider", "base_url", "model"):
            kw.setdefault(key, str(raw.get(key) or ""))
        ep = cls(**kw)
        # ★ 只有配置里**真的写了** capabilities/image 才覆盖；否则必须保持 None，
        #   让 ImageProvider.capability() 回落到适配器默认 ——
        #   否则所有端点都会吃 dataclass 的默认值（free_size=True），
        #   云端尺寸就永远不会被吸附到预设档，发出去直接 400。
        declared = raw.get("image") or raw.get("capabilities")
        if declared:
            ep.image = ImageCapability.from_dict(declared)
        if not ep.id:
            ep.id = f"{ep.provider or 'image'}-{index + 1}"
        if not ep.display_name:
            ep.display_name = str(raw.get("display_name") or ep.provider or ep.id)
        return ep

    def with_params(self, extra: dict) -> "ImageEndpoint":
        return replace(self, params={**(self.params or {}), **extra})


@dataclass
class GenRequest:
    """一次出图请求。与具体后端无关。"""

    prompt: str
    negative: str = ""
    width: int = 832
    height: int = 1216
    params: dict = field(default_factory=dict)   # 端点级附加 body（steps/cfg/hires…）
    purpose: str = "group"                       # private | group | qzone
    intent: str = ""                             # 原始意图（日志/活动流水用）
    mode: str = ""                               # solo | duo | scenery


@dataclass
class ImageResult:
    """一个后端产出的图。data 一律是**已解码的字节**（统一了 b64 / url 两种返回）。"""

    data: bytes = b""
    mime: str = "image/png"
    endpoint_id: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.data)


# 失败原因字面量 —— 面板与管理员通知按它们分流，改动请同步 test_imagegen
REASON_DISABLED = "disabled"          # 总闸关着（正常态）
REASON_QUOTA = "quota"                # 额度用完（正常态）
REASON_EMPTY = "empty-intent"         # 没给画什么（正常态）
REASON_NO_ENDPOINT = "no-endpoint"    # 一个可用后端都没有（故障态）
REASON_BACKEND_DOWN = "backend-down"  # 负缓存里记着它刚挂过（故障态）
REASON_TIMEOUT = "timeout"
REASON_HTTP = "http-error"
REASON_EMPTY_IMAGE = "empty-image"
REASON_TOO_LARGE = "too-large"
REASON_BAD_CONFIG = "bad-config"
REASON_FAILED = "failed"

# 「正常态」原因 = 不该给用户发失败话，也不该通知管理员（避免刷屏）
BENIGN_REASONS = frozenset({REASON_DISABLED, REASON_QUOTA, REASON_EMPTY})


@dataclass
class GenOutcome:
    ok: bool = False
    data: bytes = b""
    path: str = ""                   # 成功时落盘后的绝对路径（失败为空）
    reason: str = ""
    detail: str = ""                 # 给人看的具体原因（进日志/管理员通知）
    endpoint_id: str = ""
    tried: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def benign(self) -> bool:
        """正常态失败（关着 / 额度满 / 没说要画）—— 静默处理即可。"""
        return self.reason in BENIGN_REASONS
