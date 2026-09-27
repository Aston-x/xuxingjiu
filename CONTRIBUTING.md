# 贡献指南

谢谢愿意帮忙。为了让 PR 顺利合进来，请先看这几条。

---

## 最重要的一条：别提交密钥和个人数据

提交过就等于泄露过，删文件救不回来。推之前跑一次：

```bash
bash tools/preflight.sh
```

它会把「明文密钥 / Cookie / 真实 QQ 号 / 本机绝对路径 / 不该跟踪的配置文件」全查一遍。

**绝不能提交**（`.gitignore` 已挡，别用 `git add -f` 绕过）：

```
qqbot/config.json          qqbot/state/           qqbot/*_state.json
qqbot/memory.json          qqbot/mood.json        qqbot/bili_state.json
qzone-bridge/.env          qzone-bridge/test_cache/
*.log                      NapCat/ 本体
```

写文档举例子时用 `sk-xxxx`、`123456789`（或项目里那套合成号 `10001/10002`）这类占位。

---

## 开发环境

```bash
git clone <你的 fork>
cd xuxingjiu

# Python 侧
cd qqbot
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt      # Windows: .venv\Scripts\python.exe

# Node 侧（只在改 qzone-bridge 时需要）
cd ../qzone-bridge && npm ci
```

---

## 改完必须跑的测试

```bash
cd qqbot
.venv/bin/python test_regression.py    # 全量回归（含 Provider / 生图 / 跨平台段）
.venv/bin/python test_providers.py     # 模型层，纯离线
.venv/bin/python test_imagegen.py      # 生图层，纯离线

cd ../qzone-bridge
npm run typecheck && npm run test:unit
```

**三条硬要求**：

1. 测试必须**全绿且通过数不减**。这套回归里每一条断言都对应一个踩过的坑，
   数字掉下来通常意味着你改坏了一个已有行为。
2. 测试必须**离线**、且**不碰真实状态文件**。
   测试会把可写路径重定向到临时目录，结束时做指纹自检 —— 这条别绕。
3. 新增功能要**补用例**。尤其是异常路径（超时 / 403 / 返空 / 磁盘满），
   这些才是真出问题的地方。

---

## 代码风格

- 注释写**为什么**，不写**是什么**。这个项目里几乎每条注释都在解释"当年踩了什么坑"，
  照这个风格写比写"设置变量 X"有用得多。
- 中文注释、中文文档没问题（项目就是中文的）。
- 单文件已经很大了（`bot.py` 9000+ 行），**新功能优先拆成独立模块**，
  比如模型层在 `providers/`、生图层在 `imagegen/`、跨平台工具在 `filelock.py`。
- 别引入重依赖。当前直接依赖只有 4 个（httpx / websockets / pillow / qrcode），
  `psutil` 是**可选**的（没有也要能跑）。

---

## 两个容易改错的地方

### 1. 控制台面板是「三处接线」

新增一个面板必须**同时**改：

| 位置 | 改什么 |
| --- | --- |
| `qqbot/console_server.py` | `Handler._KINDS` 加路由；写操作还要进 `WRITE_OPS` |
| `qqbot/bot.py` | `console_snapshot` 加 kind 分支；写操作加 `console_apply` 分支 |
| `qqbot/public/console.html` | 卡片容器 `id="p-xxx"` + `renderXxx()` + `LOADERS` 条目 |

少一处 → 404 / 500 / **空白且无任何报错**。而且**卡片编号 `B01` 必须连续**（按页面顺序）。
回归【33】段会守这些，故意少改一处它应该立刻变红 —— 你可以先这么验一下。

### 2. 模型 / 生图端点是「列表」，不能用点路径编辑

`_get_path` 遇到 list 会返回 None，`update_config` 写带索引的路径会**把 list 覆写成 dict**
（数据损坏）。所以端点在控制台里是**只读**的，改端点请改 `config.json` 然后点「重载配置」。

### 3. 脚本编码（.bat 纯 ASCII / .ps1 带 BOM / .sh 是 LF）

这一条是「下载下来双击就炸」的头号元凶 —— 用户在新环境报
`'xxx' 不是内部或外部命令`、`'nstall.ps1' 不是内部或外部命令` 就是它。

- **`.bat` / `.cmd` 必须纯 ASCII（0 个非 ASCII 字节）**。
  `cmd.exe` 用**控制台代码页**（中文 Windows = 936/GBK）**逐行**解码批处理，
  文件前头的 `chcp 65001` 救不了它**之前**那段中文 —— 3 字节的 UTF-8 汉字会被 GBK 按 2 字节错配，
  多出来的半个字节还会把紧跟的 ASCII 一起吞掉，整篇解析错位。纯 ASCII 是唯一稳的解，
  连 `chcp` 都放前两行靠后。所有中文提示请挪到 `install.ps1`（带 BOM）或文档里。
- **`.ps1` 必须 UTF-8 with BOM**。
  PowerShell 5.1 对**无 BOM** 的 `.ps1` 按系统 ANSI 代码页解码，中文直接乱码（是解码错不是显示错）。
  BOM 同时修好 PS 5.1 和 PS 7。文件开头放个自检：若 `"许杏玖".Length -ne 3`
  就提示「本脚本没被正确解码（很可能存成了 UTF-8 无 BOM）」。
- **`.sh` 必须 LF + 无 BOM**。
  CRLF 会搞坏 shebang（`#!/bin/sh` 后面的 `\r` 被当成命令名一部分），BOM 会让 `#!` 不是文件首行。

机器守卫（别靠手改）：`tools/normalize_scripts.py` 扫全仓强制以上规则；
回归 **【37】** 段、`tools/preflight.sh` 第 6 步、CI `install-dryrun.yml` 的「脚本编码守卫」都会拦。
改完跑一次 `python tools/normalize_scripts.py` 看有没有飘红。

### 4. 依赖用「版本下限 >=」，别钉死 ==

项目要在多个 Python 上跑（实测 3.11 / 3.13，CI 另测 3.14）。`requirements.txt` 里
**只能用 `>=` 下限，不能 `==` 钉死某个版本**：pillow / websockets 这类带 C 扩展的包，
一旦钉死「在某个老 Python 上解析出的版本」，在新 Python（如 3.14）上往往没有对应的
预编译 wheel，pip 只能回退源码编译，在没装 C 编译器的机器上直接失败
（表现就是「python3.14 环境就用不了」）。

要可复现构建时：`pip freeze > requirements.lock.txt` 锁一份，用那份 lock 装即可。
升级依赖后必须跑三套测试（test_regression / test_providers / test_imagegen）。

---

## 提交 PR

- 一个 PR 做一件事。混着做会很难 review。
- 提交信息写清楚「改了什么 + 为什么」，不用管格式。
- PR 模板里那份自查清单请勾一下。
- CI 绿了才会被看。CI 挂了先自己看日志，别急着 @ 人。

---

## 报 bug 之前

```bash
qqbot/.venv/bin/python qqbot/tools/doctor.py --json
```

把这段输出贴进 issue。它会告诉你「哪一项没配、哪一项连不上」，
一半的问题在这一步就能定位。

**贴之前扫一眼有没有密钥** —— `doctor` 已经做过脱敏，但你自己的截图/日志未必。
