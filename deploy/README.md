# deploy —— 一键启动 / 停止（分平台）

## Windows

| 文件 | 作用 |
| --- | --- |
| `start-all.pyw` | 静默启动全套（qqbot → 空间桥 → NapCat），完成后自动开控制台 |
| `stop-all.pyw` | 只停 qqbot 与空间桥接，**绝不动 QQ / NapCat** |

双击即可，**不会出现终端黑窗口**（用 `pythonw.exe` 跑 `.pyw`）。

## Linux / macOS

| 文件 | 作用 |
| --- | --- |
| `start-all.sh` | 同上，用 `.venv/bin/python launcher.py start` |
| `stop-all.sh` | 同上 |
| `qqbot.service.example` | systemd **用户级**服务模板（开机自启 / 崩溃重启） |

```bash
bash deploy/start-all.sh          # 启动
bash deploy/stop-all.sh           # 停止
# 或交给 systemd：
cp deploy/qqbot.service.example ~/.config/systemd/user/qqbot.service
systemctl --user enable --now qqbot
```

> `start-all.sh` 里用的是 `exec`，所以要放后台自己加 `nohup ... &` 或用 systemd。
> 真正"不占终端"靠的是 `launcher.py` 内部的 `start_new_session=True`（等价 setsid）。

## 顺序为什么重要

这是**反向 WebSocket**：接入端（默认 NapCat）主动连到 qqbot 的 6199 端口。
所以必须**先让 qqbot 在 6199 上等着**，再拉起接入端。
`launcher.py` 已经按这个顺序编排，而且每一步都会先探测端口、核对进程的
**命令行特征**（不再只看镜像名），只补启**缺失**的组件。

## 🔴 一条跨平台通用的红线

**绝不要 `taskkill /IM QQ.exe`（Windows）或 `pkill -f QQ`（Linux/macOS）。**
你的日常号和机器人号可能用同一份 QQ 客户端安装（Windows 上尤其如此），
按名字杀会把大号一起带走。需要停进程时一律**按端口定位 → 核对命令行特征 → 才动手**，
QQ 永远不在候选列表里。`launcher.stop_*` 就是这么写的。

## 依赖

`start-all` 会检查：

1. `qqbot/launcher.py` 在不在；
2. `.venv` 装好了没（Windows `.venv/Scripts`、POSIX `.venv/bin`）；
3. `qqbot/config.json` 在不在。

Windows 上任何一步失败都会**弹消息框**（无控制台，不弹框你就什么都看不到）；
POSIX 上直接打到 stderr 并给非零退出码。
