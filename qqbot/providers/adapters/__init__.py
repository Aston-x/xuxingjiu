"""适配器注册入口。

**导入本包 = 注册全部内置适配器**（靠 `@register_provider` 的副作用）。
顺序固定，便于「同名冲突时谁赢」可预测。
"""

from __future__ import annotations

# 注册顺序 = 优先级顺序（先注册的保留名字）
from . import openai as _openai          # noqa: F401  OpenAI 兼容系（覆盖面最广）
from . import azure as _azure            # noqa: F401  Azure OpenAI
from . import anthropic as _anthropic    # noqa: F401  Claude
from . import gemini as _gemini          # noqa: F401  Google Gemini
from . import ollama as _ollama          # noqa: F401  Ollama 原生
from . import lmstudio as _lmstudio      # noqa: F401  LM Studio 原生探针

# declarative 不在 adapters/ 下（它需要清单实例化），但注册动作要在这里发生
from .. import declarative as _declarative  # noqa: F401

__all__ = []
