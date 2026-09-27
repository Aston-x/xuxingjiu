"""生图适配器注册入口。

**导入本包 = 注册全部内置生图适配器**（靠 `@register_image_provider` 的副作用）。
顺序固定，便于「同名冲突时谁赢」可预测。

本地：sd_webui（实测）/ comfyui（实测形状）/ sd_next / invokeai
云端：openai_images / dashscope / cogview / siliconflow_image / gemini_image

⚠️ 除 sd_webui 与 comfyui 外，其余后端的接口形状按**公开文档**实现，没有实测环境：
它们的 `probe()` 会自报 `unsupported`，`doctor.py` 也会标「未实测」。
宁可报"不支持"，也不要假装支持然后画错。
"""

from __future__ import annotations

from . import sd_webui as _sd_webui          # noqa: F401
from . import comfyui as _comfyui            # noqa: F401
from . import sd_next as _sd_next            # noqa: F401
from . import invokeai as _invokeai          # noqa: F401
from . import openai_images as _openai_images  # noqa: F401
from . import dashscope as _dashscope        # noqa: F401
from . import cogview as _cogview            # noqa: F401
from . import siliconflow_image as _siliconflow  # noqa: F401
from . import gemini_image as _gemini_image  # noqa: F401

__all__ = []
