# Python 插件：5 分钟接一家新厂商

## 先确认你需不需要写代码

按成本从低到高，先试前面两档：

| 情况 | 做法 | 要写代码吗 |
| --- | --- | --- |
| 厂商提供 **OpenAI 兼容端点**（绝大多数都提供） | 改 `config.json` 的 `base_url` + `model` | 不用 |
| 请求形状像 OpenAI，只是 **URL / 鉴权 / 字段名** 不同 | 往 `providers/manifests/` 加一段 JSON | 不用 |
| 报文结构完全不同 / 需要签名 / 需要先换 token | **写插件**（本文档） | 要 |

## 写一个插件

在 `providers/plugins/` 下新建 `.py`，最小骨架（可以直接抄 `example_echo.py`）：

```python
from providers.base import Provider, register_provider
from providers.types import Capability, ChatResult, Request


@register_provider("myvendor")          # ← 这个字符串就是 config 里 provider 写的名字
class MyVendorProvider(Provider):
    name = "myvendor"
    display_name = "我家的大模型"

    def default_capabilities(self) -> Capability:
        # 声明能力，调用方据此决定要不要把图换成文字、用哪个字段名传输出上限
        return Capability(vision=False, vision_input="none",
                          max_tokens_key="max_tokens",
                          temperature_range=(0.0, 1.0))

    def build_request(self, ep, messages, *, stream, max_tokens, temperature, extra) -> Request:
        # ep 是 Endpoint（含 base_url / model / auth() 等）
        # messages 已经过协议预处理（图片该换占位就换好了）
        return Request(method="POST",
                       url=ep.base_url.rstrip("/") + "/v1/chat",
                       headers={"X-Token": ep.auth(), "Content-Type": "application/json"},
                       json_body={"model": ep.model, "messages": messages,
                                  "max_new_tokens": max_tokens})

    def parse_response(self, ep, data) -> ChatResult:
        return ChatResult(text=str(data.get("reply") or "").strip(),
                          usage={}, endpoint_id=ep.id,
                          finish_reason=str(data.get("state") or ""))
```

需要时还可以覆盖这几个（都有默认实现）：

| 方法 | 什么时候要覆盖 |
| --- | --- |
| `adapt_messages(messages, ep)` | 报文结构不同（system 位置、图片块格式） |
| `needs_truncation_retry(result)` | 截断判据不是 `length` / `max_tokens` |
| `probe(ep, http)` | 想让它出现在控制台的「模型探测」里 |
| `parse_error(resp)` | 想把服务端错误原文抠得更干净 |

## 启用它

插件**默认不会被加载** —— 往目录里丢文件是不生效的，必须在配置里点名：

```json
{
  "providers": {
    "plugins": {
      "dir": "providers/plugins",
      "enabled": ["myvendor.py"],
      "allow_dir_scan": false
    }
  }
}
```

配好端点就能用了：

```json
{"id": "mv", "provider": "myvendor", "base_url": "https://api.myvendor.com",
 "model": "big-1", "tier": "cloud", "api_key_env": "MYVENDOR_API_KEY"}
```

## 安全边界（请认真读）

**插件是在主进程里 `import` 的 Python 文件，拿到就等于能执行任意代码。这里没有真沙箱。**

所以框架只保证这几件事：

1. `allow_dir_scan` 默认 `false` —— 不点名的文件不会被自动执行；
2. 每个插件独立 `try/except BaseException`，加载失败只记错误，**不会让进程起不来**；
3. 用 `spec_from_file_location` + 唯一模块名加载，不污染 `sys.modules`；
4. 启动日志会打印每个已加载插件的 **SHA256 前 12 位**，你可以自己核对；
5. 与内置适配器**同名**的插件会被跳过（不会静默覆盖内置实现）。

**白名单是唯一的信任边界。** 别人的插件先看代码再启用。

## 热重载

插件模块在 `sys.modules` 里有缓存，热重载不可靠。`provider_reload` 只重读**清单与配置**，
改了插件代码请**重启进程**。
