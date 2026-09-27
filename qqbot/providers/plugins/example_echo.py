"""示例插件：把最后一条 user 消息原样回显。

**仅用于演示注册流程**，别在生产里开。真正要接一家厂商时，请照抄这个骨架，
把 `build_request` / `parse_response` 换成目标协议的形状。

怎么启用：在 config.json 里点名（默认不扫目录）——

    "providers": {
      "plugins": { "dir": "providers/plugins", "enabled": ["example_echo.py"] }
    }

然后配一个端点：

    {"id": "echo", "provider": "echo", "base_url": "http://127.0.0.1:9999",
     "model": "-", "tier": "local"}
"""

from providers.base import Provider, register_provider
from providers.types import Capability, ChatResult, Request


@register_provider("echo", priority=10)
class EchoProvider(Provider):
    name = "echo"
    display_name = "回显示例（演示用）"

    def default_capabilities(self) -> Capability:
        return Capability(vision=False, vision_input="none", streaming=False)

    def build_request(self, ep, messages, *, stream, max_tokens, temperature, extra) -> Request:
        return Request(method="POST", url=ep.base_url.rstrip("/") + "/echo",
                       headers={"Content-Type": "application/json"},
                       json_body={"messages": messages, "max_tokens": max_tokens})

    def parse_response(self, ep, data) -> ChatResult:
        last = next((m.get("content") for m in reversed(data.get("messages") or [])
                     if m.get("role") == "user"), "")
        return ChatResult(text=str(last or ""), usage={}, endpoint_id=ep.id,
                          finish_reason="stop", raw=data)
