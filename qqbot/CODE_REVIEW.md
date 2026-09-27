# bot.py 代码体检报告

体检对象：`qqbot/bot.py`（2567 行 / 126 个函数 / 12 个类）
方法：AST 静态分析 + 调用次数统计 + 重复块扫描 + 配置项引用核对
说明：本报告只列**确实的问题**，不改动任何代码。

---

## 一、真·死代码（可直接删除）

### 1. `config.json` 的 `appearance` 段 —— 全文件 0 处引用

```json
"appearance": "黑发长直，头顶一对黑猫耳，琥珀金色眼睛，脖子上系着一条黑色颈带挂一枚旧金铃…"
```

**它原本是做什么的**：最早做"图片生成"时，用它给模型描述她长什么样。

**为什么变成冗余**：后来出图改用 `sd.character_tags`（英文 tag，给 Stable Diffusion 用的：
`1girl, solo, long straight black hair, black cat ears, amber eyes, ...`）。
一个中文描述、一个英文 tag，功能重叠，**代码只读英文那份**，中文这份从此没人看。

→ 建议删除，或把它改成注释性质留在 `sd.character_tags` 旁边做对照。

### 2. `from __future__ import annotations`（第 6 行）

`__future__` 导入是给 Python 3.7–3.9 用的（让类型标注支持前向引用）。
开发环境是 **Python 3.11**，这个特性早已是默认行为，等于空操作。

→ 无害，但可以删。

---

## 二、重复实现（同一件事写了两遍，可合并）

### 1. 禁言执行逻辑两套 ⚠️ 最值得合并

| 位置 | 函数 | 用途 |
|---|---|---|
| 第 1398 行 | `apply_bans()` | 她自己决定禁言（`[禁言:昵称 分钟]`） |
| 第 1432 行 | `force_ban()` | 连戳自动禁言（绕过每小时上限） |

两个函数各自完整实现了：**查管理员身份 → 调 `set_group_ban` → 记日志**，约 20 行几乎一样。

**历史原因**：`apply_bans` 是最早写的；后来做"连戳 3 次自动禁言"时，需要绕开每小时次数上限，
我另写了 `force_ban`，把管理员保护和禁言调用**又抄了一遍**。

→ `apply_bans` 内部应该直接调 `force_ban`，只保留"解析标记 + 次数限制"这部分自己的逻辑。

### 2. `feed_lines()` 与 `raw_feeds()` 重复构建请求参数

| 位置 | 函数 | 返回 |
|---|---|---|
| 第 926 行 | `raw_feeds()` | 原始条目（点赞要用元数据） |
| 第 1019 行 | `feed_lines()` | 格式化好的文字行 |

两处都有这段一模一样的参数拼装：

```python
action = "get_friend_feeds" if friend else "get_emotion_list"
params: dict = {"num": num, "include_image_data": False}
if not friend:
    params.update({"user_id": OB.self_id, "pos": 0})
```

**历史原因**：`feed_lines` 是先写的（只读展示）；后来为点赞加了 `raw_feeds`
（需要原始元数据 + 带缓存），但没回头让 `feed_lines` 复用它。

→ `feed_lines` 应该改成调 `raw_feeds()` 拿数据再格式化。

### 3. 表情包发送块重复 3 次

第 **1970**、**2334**、**2471** 行，分别在 `handle_message` / `idle_thought` / `proactive_speak` 里：

```python
sticker_path = None
if STICKERS.send_enable and STICKERS.items:
    m = STICKER_TAG.search(reply)
    if m:
        sticker_path = STICKERS.pick(...)
        reply = drop_tags(reply, STICKER_TAG)
```

**历史原因**：四个入口是分批长出来的，每加一个入口就把上一条的代码复制过来改。

### 4. `Memory.load()` 与 `Mood.load()` 结构雷同

第 **631** 行、第 **1195** 行。两个类的状态加载都是"文件存在→读 JSON→出错打警告"。

→ 可以抽成一个共用的 `load_json(path, default)` 工具函数。

### 5. 每日额度判断有两套写法

| 位置 | 写法 |
|---|---|
| 第 1288 行 `QZONE.can_post()` | `used_today() < max_per_day` |
| `post_qzone()` 内部 | `0 < max_per_day <= used_today()` |

同一个"今天还能不能发"的判断，一个用 `<` 正着写、一个用 `<=` 反着写，
而且 `can_post` 多判了 `prob`、`post_qzone` 多判了 `count_manual_posts`。

**历史原因**：`can_post` 是最早"自发发说说"用的闸；后来加"被要求发也计数"时，
直接在 `post_qzone` 里重写了一遍判断。

---

## 三、结构性重复：四个入口各自复制了一整套流水线 ⚠️ 最大的一处

| 入口函数 | 行号 | 规模 |
|---|---|---|
| `handle_message` | 1815 | 约 197 行 |
| `handle_notice`（戳一戳） | 2387 | 约 109 行 |
| `idle_thought`（冷场自语） | 2242 | 约 61 行 |
| `proactive_speak`（主动插话） | 2304 | 约 53 行 |

这四个入口**各自都做了**同一串事：

```
取生活状态 LIFE.current()
取天气 WEATHER.text()
组装系统提示 build_system()
   ↓ 模型生成 ↓
清理 clean_reply()
剥标记（LIKE / QZONE_READ / QZONE / DRAW）
防复读 avoid_repeat()
截断 max_reply_chars
发送 send()
```

其中「**剥标记 → 防复读 → 截断**」这三步在四个入口里几乎逐行相同。

**历史原因**：功能是一轮一轮加的 —— 先有群消息回复，再加冷场自语，
再加群友聊天时主动插话，最后加戳一戳。每次都是复制上一个入口改改，
所以同一条修复（比如今天修的 `messages` 未定义、换行处理、标点被吞）**要在四个地方分别改**。
事实上今天那两个 bug（`messages` 未定义、`.strip` 吞标点）**就是因为只改了部分入口才漏出来的**。

→ 建议抽一个共用的 `finish_reply(reply, life, user_id, key, is_group, group_id)`，
把「剥标记 → 防复读 → 截断」收进去。**预计可省 60–80 行，同时消除"改一处漏三处"的风险。**

---

## 四、命名与可读性（非冗余，但是隐患）

`norm_text()`（第 297 行）与 `normalize_reply()`（第 339 行）**名字太像，职责完全不同**：

| 函数 | 用途 | 副作用 |
|---|---|---|
| `norm_text` | 去标点去空白，**只用于复读比对** | 无 |
| `normalize_reply` | 清理零宽字符/多余空白，**换行要保留** | 影响实际发送内容 |

今天排查"她少说一两个字"时，差点误判成这两个函数打架。
→ 建议把 `norm_text` 改名成 `dedupe_key()` 之类，一眼看出是算指纹用的。

---

## 五、经核实**不是**冗余的（澄清）

| 项目 | 结论 |
|---|---|
| `WEATHER.text()` 全文件 6 次调用 | 内部有 `cache_hours` 缓存，不会重复发请求 |
| 各 `_break_line` / `_break_weak` / `active_speaker_count` 等只调 1–2 次 | 正常的小工具函数，不该合并 |
| `feed_text()` 包一层 `feed_lines()` | 合理的便利封装 |
| 各 `_deny(...)` 多行调用 | 参数不同，不是重复 |

---

## 六、处理建议（按性价比排序）

| 优先级 | 项目 | 收益 | 风险 |
|---|---|---|---|
| 1 | 抽 `finish_reply()` 合并四入口 | 省 60–80 行，杜绝"改一处漏三处" | 中（动主流程，需逐一回归） |
| 2 | `apply_bans` 复用 `force_ban` | 省约 20 行，禁言行为统一 | 低 |
| 3 | 删 `appearance`、`__future__` 导入 | 清掉真死代码 | 极低 |
| 4 | `feed_lines` 复用 `raw_feeds` | 省约 6 行 | 低 |
| 5 | 表情包块抽函数 | 省约 16 行 | 低 |
| 6 | `norm_text` 改名 | 可读性 | 极低 |

**总计可减少约 100–130 行**（约占 2567 行的 4–5%）。

需要注意：这个项目**没有 git**，改之前建议先备份 `bot.py`，或者我帮你初始化一个 git 仓库，
这样每一步重构都能回退。


---

# 整改结果（2026-09-25 执行）

已按本报告全部整改，并建立 git 仓库以便回退（基线提交 `a86212d`）。

## 实测效果

| 指标 | 整改前 | 整改后 |
|---|---|---|
| 重复的三行代码块 | **18 组** | **4 组** |
| 重复块涉及行数 | 111 行 | 约 20 行 |
| 总行数 | 2567 | 2561 |
| 回归测试 | — | **35 项全过** |

**关于"预计可减 100-130 行"没兑现，需要说明**：把重复代码抽成共用函数，
总行数基本持平 —— 代码从"复制 N 份"变成"一份函数 + N 处调用"，
省下的是重复量而不是总行数。真正的收益是**重复块 18→4 组**，
以及消除了"改一处漏三处"这一类 bug。

## 逐项落实情况

| 项目 | 处理 |
|---|---|
| `appearance` 死配置 | 已删除 |
| `from __future__ import annotations` | 已删除 |
| 禁言逻辑两套 | `apply_bans` 改为复用 `force_ban`，只保留"解析标记 + 记额度" |
| `feed_lines` 重复拼参数 | 改为复用 `raw_feeds` |
| 表情包块 ×3 | 抽成 `pick_sticker()`，四处入口共用 |
| `Memory/Mood/SD/Qzone/StickerBook` 的 load/save | 抽成 `read_json_dict()` / `write_json_dict()`，5 对全部统一 |
| 额度判断两套 | 统一到 `Qzone.over_quota(manual)` |
| `norm_text` 命名混淆 | 改名 `dedupe_key()` 并注明"不用于输出" |
| 四入口流水线重复 | 抽 `apply_reply_tags()` / `finish_reply()` / `truncate_reply()`，四处共用 |
| `StickerBook` 图片尝试块 ×2 | 抽成 `_image_attempts()` |

## 顺带修掉的一个潜在 bug

`proactive_speak`（她主动插话）**以前不跑标记流水线**，万一她写出 `[说说:]` 会**原样发进群里**——
和之前修过的"戳一戳标记泄漏"是同一类问题。现在四个入口统一走 `finish_reply`，这个洞堵上了。

## 新增：生图额度和额度拆分

`sd.max_per_day_chat = 10`（发到群聊/私聊）、`sd.max_per_day_qzone = 1`（配说说发空间），
两个计数独立落盘（`sd_state.json` 用 `{"date","chat","qzone"}`），跨天各自归零。

## 新增：回归测试 `test_regression.py`

35 项断言，覆盖文本处理、语义分句、权限、出图额度、说说额度、禁言、戳一戳、引用、
`/clear`、状态文件读写。**以后改代码先跑它**（约 3 秒）。

## 回滚方式

```bash
cd <项目根>/qqbot
git log --oneline          # 看提交
git checkout <提交号> -- bot.py config.json   # 回退到某一步
```
另有 `_backup/` 目录存着重构前的原始 `bot.py` 和 `config.json`。
