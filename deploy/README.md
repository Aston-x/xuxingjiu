# deploy —— 一键启动 / 停止

| 文件 | 作用 |
| --- | --- |
| `start-all.pyw` | 静默启动全套（qqbot → 空间桥 → NapCat），完成后自动开控制台 |
| `stop-all.pyw` | 只停 qqbot 与空间桥接，**绝不动 QQ / NapCat** |

双击即可，**不会出现终端黑窗口**（用 `pythonw.exe` 跑 `.pyw`）。

## 顺序为什么重要

这是**反向 WebSocket**：NapCat 主动连到 qqbot 的 6199 端口。所以必须
**先让 qqbot 在 6199 上等着**，再拉起 NapCat。`launcher.py` 已经按这个顺序编排，
而且每一步都会先探测端口、核对进程镜像名，只补启**缺失**的组件。

## 为什么不用 .bat

`.bat` 必然带一个控制台窗口。`.pyw` 由 `pythonw.exe` 执行，天生无窗口，
日志也全部落到 `qqbot/bot_boot.log` 与控制台的「日志」页。

NapCat 自己的 `launcher-user.bat` 是上游提供的、必须有窗口（登录 QQ 时要看提示），
所以那一步我们只在需要时提示你去手动双击，不代替它。

## 依赖

`start-all.pyw` 会检查：

1. `qqbot/launcher.py` 在不在；
2. `qqbot/.venv` 装好了没 —— 没装会弹框告诉你怎么装；
3. `qqbot/config.json` 在不在 —— 没有会提示你先从 `config.example.json` 复制。

任何一步失败都会**弹消息框**（无控制台，不弹框你就什么都看不到），
而不是静默失败。
