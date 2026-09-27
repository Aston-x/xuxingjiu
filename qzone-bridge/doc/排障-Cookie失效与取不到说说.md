# 排障：Cookie 失效 / 取不到说说

按「现象 → 根因 → 处置」写。先看第 0 节的决策树，能省掉一半时间。

> 本文里的 QQ 号一律写成占位符（`1000000001` 这种），请替换成你自己的。

---

## 0. 快速判定

```
取不到数据
├─ GET http://127.0.0.1:5700/status 看 cookie.valid
│   ├─ false  → 第 1 节：Cookie 无效（最常见，占九成）
│   └─ true   → 继续
├─ 接口返回 code: -3000 "请先登录空间"？
│   └─ 是 → 第 1 节
├─ feeds3 返回约 113 字节的空壳、解析 0 条？
│   └─ 是 → 第 1 节（Cookie 看似有效但 p_skey 无效）
└─ 好友动态能拉到、只有「指定某人」拉不到？
    └─ 第 2 节：bot 身份下的 scope/uinlist 限制
```

---

## 1. Cookie 无效

### 症状

| 检查点 | 无效时的表现 |
| --- | --- |
| `/status` | `cookie.valid === false`、`p_skey === false` |
| 接口返回 | `code: -3000, message: "请先登录空间"` |
| feeds3 响应体 | 约 113 字节的空壳，解析出 0 条 |

### 根因

QQ 空间的鉴权**完全依赖 Cookie**，其中最关键的是 `p_skey` ——
所有 `g_tk` 都是由它算出来的。它比 `skey` 过期得更快，而且**换设备登录、
改密码、异地登录都会让它立刻失效**。

### 处置（按推荐顺序）

1. **先看 `/status`**：`http://127.0.0.1:5700/status`，确认 `cookie.valid` 与 `p_skey`。

2. **更新 Cookie**（最快）：
   - 浏览器登录 QQ 空间 → F12 → Network → 随便点一个请求 → 复制整条 Cookie；
   - 必须包含 `p_skey`、`skey`、`p_uin`、`uin`；
   - 写进 `.env` 的 `QZONE_COOKIE_STRING`（**不要**用 `QZONE_COOKIE`，两处同名会打架）；
   - 重启 bridge。

3. **扫码登录**（最省事，但要装 Playwright）：
   - 设 `QZONE_ENABLE_QR=1` → 重启 → 扫码；
   - 成功后 Cookie 会自动写回 `.env`。

4. **清了缓存重来**：已配了 Cookie 仍 invalid 时，删掉 `test_cache/cookies.json`，
   只留环境变量里的 Cookie 再重启 —— 让 bridge 从环境变量重新加载并校验一次。
   （大概率是缓存里的旧 Cookie 覆盖了环境变量里的新的。）

### ⚠️ Cookie 是账号级凭据

拿到你的 `p_skey` 等于拿到你的空间控制权。**不要**把它写进任何会进版本库的文件，
不要贴给别人，不要放进聊天记录。`.gitignore` 已经挡了 `.env` 与 `test_cache/`，
但你手动复制到别处它管不了。

---

## 2. Cookie 有效，但「指定某人」拿不到说说

### 现象

- **好友动态**（混合流）：正常，有数据；
- **指定某个用户的说说列表**：永远「暂无说说」。

### 根因对比（实测）

| 场景 | 请求方式 | 结果 |
| --- | --- | --- |
| 好友动态 | `feeds3`：`uin=`**当前登录号**、`scope=0`、**不传 uinlist**，带 `outputhtmlfeed=1` / `pagenum` / `begintime`（从 externparam 解析）翻页 | 返回完整 HTML，解析出多条 ✅ |
| 指定用户·策略 1 | `feeds3`：`uin=`**目标**、`scope=1`（个人说说） | 用 bot 的 Cookie 请求**别人的**个人页，后端对非本人常返回空（113 B）❌ |
| 指定用户·策略 2 | `feeds3`：`uin=`当前登录号、`scope=0`、`uinlist=目标` | 该环境下后端不支持，返回空 ❌ |
| 指定用户·策略 3 | `feeds3`：`uin=`目标、`scope=0` | 等价于"用 bot 的 Cookie 看目标的动态"，权限不足 ❌ |

**结论**：bot 身份下唯一稳定有数据的是「当前登录号 + `scope=0` + 无 `uinlist`」的好友动态流。

### 处置思路：从能拿到的流里筛

不要依赖 `scope=1` 或 `uinlist`（后端对非本人不支持），改成：

1. 先拉**好友动态**（与 `getFriendFeeds` 完全一致：`scope=0`、游标翻页、不传 `uinlist`）；
2. 在内存里按 `uin` 过滤，只留目标用户的条目；
3. 不够就用返回的 `next_cursor` 继续翻页 + 过滤，直到凑够或没有更多页；
4. 仍为 0 条才回退到策略 2/3 兜底（该用户近期确实没动态时会命中这一支）。

### 实现要点（改代码时注意）

- 在 `getEmotionListViaFeeds3` 里，`!isOwn` 时优先走
  `fetchFeeds3Html(this.qqNumber!, false, 0, 50)` + `parseFeeds3Items(..., targetUin, ...)`；
- **必须与 `getFriendFeeds` 共用缓存**（`forceRefresh=false`、同一 cacheKey）。
  用 `true` 会强制刷新，可能用空响应覆盖掉有效缓存，表现就是"动态有数据、指定用户没有"，
  或者反过来污染缓存；
- 过滤时以 `feed_data` 的 **`data-uin`** 为准；`id="feed_*"` 块用来补 appid / 时间戳，
  **缺失时仍保留**该条（新版模板会缺），混流风险交给 `data-uin ≠ filterUin` 这层过滤兜住。

相关源码：`src/qzone/feeds3/`（解析）、`src/qzone/client.ts`（请求）。

---

## 3. 结果里混进了别人的说说

### 怎么确认

`qzone_get_posts`（NapCat 工具）返回的**每一条都会带 `uin=xxx`**，没有就显示 `uin=?`。
拿到结果先扫一遍 `uin`：

- `uin` = 目标号 → 正常（也可能是他转发的，看内容判断）；
- `uin` = **你自己的号** → 说明 `opuin` 过滤或 feeds3 解析有问题，回去查第 2 节；
- `uin=?` → 多半是 **openclaw-gateway 没加载最新插件**，重启它再拉一次。

### 为什么用 `data-uin` 而不是昵称

昵称会改、会重复、可能含 emoji，**只有 uin 是稳定的**。所有过滤都以 uin 为准。

---

## 4. 常用诊断入口

| 用途 | 怎么做 |
| --- | --- |
| Cookie 是否有效 | `GET http://127.0.0.1:5700/status` |
| 看接口原始响应 | 看 `test_cache/diagnostics/` 下的抓包轨迹（`parse_trace/*.json`） |
| 验证各端点连通性 | `npm run verify`（只读模式加 `--skip-write`） |
| 验证 HTTP 桥接 | `npm run verify:http` |
| 跑单元测试 | `npm run test:unit` |
| 类型检查 | `npm run typecheck` |

> `test_cache/` 里可能有你账号的 Cookie、好友列表与抓包内容 ——
> 它在 `.gitignore` 里，**别手动往仓库里加**。

---

## 5. 还是不行

按这个顺序排除：

1. `/status` 的 `cookie.valid` 真的是 `true` 吗（不是"看起来登录了"）；
2. 目标用户的**空间隐私设置**：仅好友可见 / 仅自己可见时，非好友怎么都拿不到；
3. 该用户是不是**长期没发动态**（第 2 节的兜底分支就是为这种情况准备的）；
4. 网络出口：部分 CDN 域名（`photo.store.qq.com`、`qpic.cn`、
   `qzonestyle.gtimg.cn`）被代理拦了会导致图片 502 —— 把这些设为 DIRECT，
   见 [`../docs/SERVER_DEBUG.md`](../docs/SERVER_DEBUG.md)；
5. 频率：短时间内大量请求会被限流（表现为接口返回空或 500），
   降低频率或换个时间再试。
