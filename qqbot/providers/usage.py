"""usage 字段归一化。

各家返回的计费字段完全不一样，归一化成 `{in, out, hit, miss}` 四个键，
面板上的「缓存命中率」才不会换个厂商就静默归零：

  · DeepSeek  : usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
  · OpenAI    : usage.prompt_tokens_details.cached_tokens（没有 miss 字段）
  · Anthropic : usage.cache_read_input_tokens
  · Gemini    : usageMetadata.cachedContentTokenCount

映射规则写在 Capability.usage 里（支持点路径），这里只负责取数和转 int。
"""

from __future__ import annotations

from .types import Capability, dget


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(data: dict, cap: Capability) -> dict:
    keys = cap.usage
    out = {
        "in": _as_int(dget(data, keys.prompt, 0)),
        "out": _as_int(dget(data, keys.completion, 0)),
        "hit": _as_int(dget(data, keys.cache_hit, 0)) if keys.cache_hit else 0,
        "miss": _as_int(dget(data, keys.cache_miss, 0)) if keys.cache_miss else 0,
    }
    return out
