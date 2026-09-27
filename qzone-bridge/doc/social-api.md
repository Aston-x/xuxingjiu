# 社交互动接口

范围就是四件事：点赞、取消点赞、评论（含回复）、删除评论。转发接口源码里也有，但它更贴近说说，放在这篇最后当邻居提一句。

写之前先说清楚一件事：这篇里的每个 URL 和参数都是从 `src/qzone/client.ts` 里扒出来的，不是从历史抓包文档抄的。历史文档和现在的实现有几处对不上，碰上不一致的地方我会指出来，不替谁圆。通用参数（`g_tk`、`qzreferrer`、`appid`、`unikey` 这些）的含义见 [api-overview.md](api-overview.md)，这里不重复。

## 实现状态总表

| 功能 | 主端点 | 方法 | 当前实现状态 |
|------|--------|------|--------------|
| 点赞 | `likes/internal_dolike_app` | POST | 已实现，三级降级 |
| 取消点赞 | 同上 + `like_cgi_likev6(optype=1)` | POST | 已实现，三级降级 |
| 移动端点 | `mobile.qzone.qq.com/like` | POST | 已实现但实机 404，等于没有 |
| 点赞列表 | 无 JSON 端点，走 feeds3 HTML | GET | 已实现，feeds3 only |
| 点赞/评论计数 | `user/qz_opcnt2` | GET | 已实现 |
| 评论读取 | 无 JSON 端点，走 feeds3 HTML | GET | 已实现，feeds3 为主路径 |
| 评论读取探测 | `emotion_cgi_getcmtreply_v6` / `mobile get_comment_list` | GET | 只在 `probeApiRoutes()` 里探活，不作数据源 |
| 评论发布 / 回复 | `emotion_cgi_re_feeds` | POST | 已实现，两条通路 |
| 删除评论 | `cgi_qzsharedeletecomment` → `emotion_cgi_delcomment_ugc` | POST | 已实现，带 sns/taotao 两级 |
| 转发 | `emotion_cgi_forward_v6` → `re_feeds` | POST | 已实现，见 [emotion-api.md](emotion-api.md) |

一句话总结现状：写操作（点赞、评论、删评）走的还是老的 PC JSON cgi，能跑；读操作（点赞列表、评论列表）在实机上早就被 `-10000` 和 500 打穿了，现在全都靠 feeds3 那页 HTML 解析。

## 术语对照

这块最容易看晕：同一个东西在不同端点上叫不同名字，同一份代码里还混着中文文档的旧叫法。先对齐一遍，后面看参数表能省不少来回。

| 概念 | 各处的写法 | 出处 |
|------|-----------|------|
| 说说 id | PC 叫 `tid`，移动端叫 `cellid`，点赞接口里叫 `fid` | `client.ts:3123`、`client.ts:4128` |
| 帖子归属键 | `topicId`，PC 通路是 `{作者号}_{tid}`，h5 通路多一个 `__1` 后缀 | `client.ts:4238`、`client.ts:4262` |
| 评论 id（删） | `comment_id` | `client.ts:4302`、`client.ts:4338` |
| 评论 id（回复） | `commentId`（大写 I），同时旁边还有一个 `commentUin` | `client.ts:4244`、`client.ts:4318` |
| 评论 id（解析结果） | `commentid`，全小写无下划线 | `feeds3/comments.ts:45` |
| 点赞定位键 | `unikey` 和 `curkey`，形如 `http://user.qzone.qq.com/{号}/mood/{tid}` | `client.ts:4094`、`client.ts:4192` |
| 操作者 / 目标作者 | `opuin` 是自己，`ouin` 是帖子作者 | `client.ts:4127` |

`comment_id`、`commentId`、`commentid` 三个写法指的都是评论 id，但不是一个东西：前两个是接口参数名，第三个是解析结果的字段名；而且从 feeds3 解析出来的那个是帖内序号，不是真实 id（后面细说）。

返回值里还有个 `_source` 字段，是仓库自己加的来源标记，取值和含义：

| `_source` | 什么时候出现 |
|-----------|-------------|
| `official_api` | 走通的官方 JSON 接口 |
| `feeds3_html` | feeds3 HTML 解析出来的原始条目 |
| `feeds3` / `feeds3_cache` | 评论或点赞列表来自解析结果，后者是命中缓存 |
| `act_all_html` | 来自 `feeds_html_act_all` 那条分页链路（`client.ts:1089`） |
| `playwright_feed` | 浏览器里拦截到的动态 |
| `derived` | 没有原始数据，靠计数之类的信息推出来的（如 `client.ts:3860`） |

这个字段在排查「数据到底从哪来」的时候最有用，`client.ts` 里到处都在传它。

## 点赞

入口是 `likeEmotion()`，`client.ts:4103`。

```typescript
async likeEmotion(
  ouin: string, tid: string, abstime: number, appid = 0, typeid = 0,
  unikeyOverride?: string, curkeyOverride?: string,
): Promise<ApiResponse>
```

只有 `tid` 是硬要求。`ouin`、`appid`、`typeid`、`abstime` 缺了会先去 `postMetaCache`（按 tid 索引，容量 1000）里找，找到了就补；`unikey` / `curkey` 也一样。补不上时 `appid` 兜底成 `311`，`unikey` 由 `buildUnikey()` 合成：

```typescript
appid === 311 ? `http://user.qzone.qq.com/${ouin}/mood/${tid}`
              : `http://user.qzone.qq.com/${ouin}/app/${tid}`  // 源码自注：通常不准
```

`curkey` 缺省等于 `unikey` 自己。这两个值从 feeds3 HTML 里带出来（`likeUnikey` / `likeCurkey`）才靠谱，合出来的只在 appid=311 的普通说说话上能对。

### 方法 1：internal_dolike_app

```
POST https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?g_tk={g_tk}
Content-Type: application/x-www-form-urlencoded
Referer: https://user.qzone.qq.com/{自己号}/main
```

| 参数 | 值 | 说明 |
|------|-----|------|
| `qzreferrer` | `https://user.qzone.qq.com/{自己号}/main` | 与 Referer 一致 |
| `opuin` | 自己 QQ 号 | 操作者 |
| `unikey` | `http://user.qzone.qq.com/{作者}/mood/{tid}` | 目标帖 |
| `curkey` | 同 unikey | 页面上下文 key |
| `appid` | `311` | feed 类型 |
| `typeid` | `0` | 子类型 |
| `fid` | tid | 目标帖 id |
| `from` | `1` | 固定 |
| `active` | `0` | 源码里点赞传 0 |
| `fupdate` | `1` | 立即刷新计数 |
| `abstime` | 发布时间戳 | 点赞时有，取消时没有 |
| `format` | `json` | |

判定成功的条件是 `ret === 0`，或者 `code === 0` 且 `http_status < 400`（`client.ts:4134`）。两个都看，是因为这个接口在不同环境返回的字段名不一样。

### 方法 2：like_cgi_likev6

```
POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/like_cgi_likev6?g_tk={g_tk}
```

参数换了一批：`opuin`、`ouin`、`fid`、`abstime`、`appid`、`typeid`、`key=''`、`format=json`、`qzreferrer`。判定是 `code === 0` 或 `ret === 0`。这条在 [compatibility-matrix.md](compatibility-matrix.md) 里标的是「可靠」。

### 方法 3：移动端

前两条都没成，落到 `likeMobileFeed()`（`client.ts:4190`）：

```
POST https://mobile.qzone.qq.com/like?g_tk={g_tk}
body: unikey, curkey, appid, typeid, active=0, fupdate=1
headers: 移动 UA + X-Requested-With: XMLHttpRequest
```

`compatibility-matrix.md` 给这条判的是实机 404。所以第三级基本只是把链路走完，不指望它成。

### Playwright 那条路

除了上面三级，还有个浏览器内的点赞实现 `likeWithPlaywrightContext()`（`client.ts:1750`），调用方是 `scripts/instant-like.ts` 和一个调试脚本，不在 bridge 的 OneBot action 主链路上。

它的做法分两步。先在已加载空间页的 DOM 里找 `[data-islike]` 按钮，匹配依据是 `data-unikey` / `data-curkey` / `data-detailurl` 或者最近的带 `data-tid` 的容器；命中且 `data-islike === '0'` 就 `click()`，然后等属性变成 `'1'`。点不动的话，退到在页面上下文里直接 `fetch`，顺序还是 internal_dolike_app → like_cgi_likev6 → mobile like，`credentials: 'include'` 让浏览器自己带 Cookie。

这条路依赖 `ensurePlaywrightFeedSession()`，开关是环境变量 `QZONE_INSTANT_LIKE_PLAYWRIGHT`，默认开（`src/qzone/config/env.ts:49`）。另有一个 `QZONE_INSTANT_LIKE_SOURCE`，`env.ts:50` 里默认 `playwright_first`，但我在 `src/` 里没找到消费它的地方，像是留了口子没接上。

## 取消点赞

入口 `unlikeEmotion()`，`client.ts:4151`，参数签名和 `likeEmotion` 一模一样，降级结构也一样。逐个说差别。

方法 1 是同一个 `internal_dolike_app`，但这里有个必须点出来的地方：它的请求体在 `client.ts:4172` 长这样——

```typescript
new URLSearchParams({
  qzreferrer, opuin, unikey, curkey, appid, typeid,
  fid: tid, from: '1', active: '0', fupdate: '1', format: 'json',
})
```

跟点赞那次的唯一区别是少了 `abstime`，`active` 仍然是 `'0'`。也就是说，从源码上看，点赞和取消点赞打的是同一个 URL、同一组参数，只差一个 `abstime`。而 [fallback-strategy.md](fallback-strategy.md) 的取消点赞一节写的是「`active=0` 取消」——照它的说法，那点赞也用 `active=0` 就冲突了。

这两份东西对不上，我按源码写，但结论标为未实测：目前没有证据说明 internal_dolike_app 是靠哪个字段区分点赞和取消的。真实浏览器抓包里取消点赞大概率是 `active=1`，但仓库里没有对应的抓包记录，我不能替它编。这条要动之前建议先抓一次包。

方法 2 是 `like_cgi_likev6`，参数 `opuin` / `ouin` / `fid` / `abstime` / `appid` / `typeid` / `optype=1` / `format=json` / `qzreferrer`。判定只看 `code === 0`，没看 `ret`（`client.ts:4184`），比点赞那侧窄一点。

方法 3 是 `likeMobileFeed(ouin, tid, appid, typeid, 1)`，也就是 `active=1`。

顺带说一句 `client.ts:523` 那个 `routes` 对象，初始值是：

```typescript
routes: Routes = {
  comments: 'pc', detail: 'pc', delete_comment: 'pc',
  unlike: 'mobile_like_active_1',
};
```

`unlike` 这个键写着 `mobile_like_active_1`，但 `unlikeEmotion()` 里没有读它，代码始终从 internal_dolike_app 开始试。这个键目前是摆设。

## 点赞列表

没有可用的 JSON 端点。`getLikeList()`（`client.ts:4537`）直接转给 `getLikeListBestEffort()`，而后者只走 feeds3 HTML。

具体流程在 `client.ts:3756`：

1. 先查 `feeds3LikesCache`，命中就返回。
2. 没命中就拉 `feeds3_html_more`，`scope=1`、`count=50`（拉作者空间首页）。
3. 解析完还没拿到，并且目标不是自己，再拉一次 `scope=0`、`filter=all`、`applist=all`（好友动态流）。
4. 都没有就返回空数组，并打一条日志说明是 `post_counts_present_but_not_embedded`（有计数但 HTML 没展开点赞人）还是 `html_no_matching_like_bucket`。

解析函数是 `parseFeeds3Likes()`（`src/qzone/feeds3/likes.ts:58`），返回 `Map<tid, Feeds3Like[]>`。`Feeds3Like` 字段是 `uin`、`nickname`、`tid`、`ownerUin`、`abstime`、`customItemId`、`_source`，`_source` 固定 `'feeds3_html'`。解析器认两种结构：一种是 `class="user-list"` 里的一串 `<a href="http://user.qzone.qq.com/{uin}">`，另一种是 `feedstype="101"` 的动态（「赞了」这类），去重时同一个 uin 保留 `abstime` 最大的那条。

有个现实约束要知道：这个列表只反映 HTML 里真正展开出来的点赞人。[fallback-strategy.md](fallback-strategy.md) 也写了，它可能和 `qz_opcnt2` 的计数对不上——计数是 10，列表里只有 3 个人是正常的。

上层 SDK 版的 `getLikesBestEffort()`（`client.ts:3815`）会额外返回一个 `availability` 字段，取值 `available_full` / `available_partial` / `not_embedded`，用帖子的 `likenum` 和实际解析出的条数比对得出。想区分「真没人赞」和「有人赞但没展开」，看这个字段。

## 计数：qz_opcnt2

`getTrafficData()`，`client.ts:4549`：

```
GET https://user.qzone.qq.com/proxy/domain/r.qzone.qq.com/cgi-bin/user/qz_opcnt2
    ?_stp={毫秒时间戳}&unikey={URL编码的unikey}&face=0&fupdate=1&g_tk={g_tk}
```

`unikey` 同样是 `http://user.qzone.qq.com/{uin}/mood/{tid}`。返回里取 `data[0].current.newdata`，字段名是缩写：`LIKE` 点赞、`PRD` 浏览、`CS` 评论、`ZS` 转发。取不到就整体返回 `{ like: -1, read: -1, comment: -1, forward: -1 }`，用 `-1` 表示未知而不是 `0`，这点在展示的时候别搞错。

## 评论：读取

`getCommentsBestEffort()`（`client.ts:3440`，实现体在 `3572`）同样以 feeds3 为主路径，但比点赞那边多绕几圈。

有 JSON 版的 PC 评论接口，`emotion_cgi_getcmtreply_v6`：

```
GET https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_getcmtreply_v6
    ?g_tk={g_tk}&uin={作者}&tid={tid}&num=5&pos=0&format=json
```

和移动端的 `mobile.qzone.qq.com/get_comment_list`（参数 `cellid` 代替 `tid`）。这两个在现在的代码里只在 `probeApiRoutes()`（`client.ts:4799`）里被请求，用来探测该走 PC 还是移动路由，请求结果不喂给数据流。`compatibility-matrix.md` 给它们的评价是「GET 空 / POST 可用」和「不稳定」，`fallback-strategy.md` 说得更直接：探针账号上多为空体 / 404 / 参数错误，不作主路径。

所以实际取评论是这样：

1. 查 `feeds3Comments` 桶，缓存新鲜度默认 90 秒（`maxCacheAgeSec`），命中就直接切片返回。
2. 没命中，先拉好友动态流（`scope=0`、`count=50`），这是多数帖所在的地方。
3. 还没命中，拉作者空间 `scope=1`、`count=50`。
4. 还是空，走 `getFeedsHtmlActAll()` 分页翻「全部动态」，每页 20 条，最多 25 页，翻到命中或 `has_more` 为假就停。

返回值带 `commentlist`、`has_more`、`next_cursor`，还有两个元字段 `_source`（`feeds3` 或 `feeds3_cache`）和 `_feeds3_total`。注意这里的 `_feeds3_total` 是「这回解析出来的条数」，不是帖子真实的评论总数，真实总数看 `postMetaCache` 里的 `cmtnum`。

这个函数有两种调用形状，返回结构不一样（`client.ts:3440` 起是重载声明）：

| 调用形状 | 返回 |
|----------|------|
| `getCommentsBestEffort(uin, tid, num, pos, options)` | `ApiResponse`，评论在 `commentlist` 里 |
| `getCommentsBestEffort({ uin, tid, num, pos, options })` | `SdkResult`，评论在 `data.comments` 里，另带 `identity` 和 `availability` |

传对象进去的那条会走 SDK 分支（`getCommentsBestEffortSdk`），只有单选发那条才走上面描述的四步降级。想要 `commentlist` 就传位置参数。

### 评论数据结构

`parseFeeds3Comments()`（`src/qzone/feeds3/comments.ts:931`）产出 `Map<tid, comment[]>`，单条评论的字段定义在 `comments.ts:44`：

| 字段 | 含义 |
|------|------|
| `commentid` | 评论 id，来自 HTML 的 `data-tid` |
| `uin` / `name` | 评论者号与昵称 |
| `content` | 正文 |
| `createtime` | 秒级时间戳，解析不出时可能为 0 |
| `pic` | 可选，评论里的图片（`qpic.cn` / `photo.store.qq.com`） |
| `is_reply` | 是不是二级回复 |
| `parent_comment_id` | 二级回复挂在哪个一级评论下 |
| `reply_to_uin` / `reply_to_nickname` | 被回复的人 |
| `reply_to_comment_id` | 被回复的那条评论（来自 `t2_tid`） |

一级评论认 `<li class="comments-item" data-type="commentroot">`，二级回复认 `data-type="replyroot"`，被包在 `mod-comments-sub` 里。解析器还做了一件事：拿块内的 `t1_tid` 锚点和当前 feed 段的 canonical tid 比，不一致就整段丢掉，防止上一条动态的评论区串到这一条上（`comments.ts:29` 那个 `commentBlockMatchesPost`）。

这里有个坑，[fallback-strategy.md](fallback-strategy.md) 专门写了一段：feeds3 里解析出来的 `commentid` 是帖内序号（1、2、3 这种），不是后端数据库的真实评论 id。拿它去删评论或者回复，可能失败。只有 PC/mobile 那两个 JSON 接口返回的才是真实 id，而那两个接口现在不可用。

## 评论：发布与回复

入口 `commentEmotion()`，`client.ts:4201`：

```typescript
async commentEmotion(
  ouin: string, tid: string, content: string,
  replyCommentId?: string, replyUin?: string,
  appid = 0, abstime = 0,
): Promise<ApiResponse>
```

进函数先做两件事。一是表情格式转换，`convertNamesToEmojis()` 把 `[微笑]` 这类文本转成 `[em]e100[/em]`。二是回复评论时自动补 @ 前缀，如果 `content` 不是以 `@{` 开头，会拼成：

```
@{uin:{被回复者QQ},nick:{昵称},auto:1} {正文}
```

昵称从 Cookie 里取（回复自己）或从 `friendCache` 取（回复好友），都取不到就用 QQ 号顶着。

然后走两级：

### ① PC user 代理的 re_feeds

```
POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds?g_tk={g_tk}
body: hostUin={作者}, topicId={作者}_{tid}, content={正文}, format=json, qzreferrer
```

回复评论时额外带 `commentId`、`replyUin`、`commentUin`（后两个值相同）。判定是 `code === 0` 或 `ret === 0`。注意这里的 `topicId` 是 `{ouin}_{tid}`，没有 `__1` 后缀，那是 h5 通路才加的。

### ② h5 兜底

```
POST https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds?g_tk={g_tk}
```

参数完全不同，是照着旧的 h5 抓包写的：`topicId` 带 `__1` 后缀、`format=fs`（不是 json）、固定一堆 `feedsType=100` / `plat=qzone` / `source=ic` / `platformid=50` / `paramstr` / `ref=feeds` / `richval=''` / `richtype=''` / `private=0` 之类的字段，`paramstr` 一级评论是 `'1'`、回复是 `'2'`。

回复时额外加的字段分两组（`client.ts:4281`）：h5 抓包那组是 `commentId` + `commentUin`；feeds3 文档那组是 `t1_uin`（帖子作者）、`t1_tid`（帖子 tid）、`t2_uin`（被回复者）、`t2_tid`（被回复评论 id）。两组都传，这是 [fallback-strategy.md](fallback-strategy.md) 记录的做法，原文的说法是「桥接同时传递两组参数」，原因也写在那儿：服务端到底认哪一组并不确定。

参数逐条含义见 [feeds3-parser.md](feeds3-parser.md) 的「评论回复 API」一节。

发布成功后，`action_send_comment`（`src/bridge/actions.ts:398`）会从返回的 HTML 片段里用正则 `data-type="commentroot"\s+data-tid="(\d+)"` 捞出最后一条的评论 id，当作 `comment_id` 返回给调用方。

## 删除评论

`deleteComment()`，`client.ts:4297`：

```typescript
async deleteComment(uin: string, tid: string, commentId: string, commentUin?: string)
```

第一件事是看 `routes['delete_comment']`。默认是 `'pc'`（没有探针会改这个键，`probeApiRoutes()` 只改 `detail` 和 `comments`），所以除非有人手工改，否则不会走移动端分支。移动端分支是：

```
POST https://mobile.qzone.qq.com/del_comment?g_tk={g_tk}
body: cellid={tid}, comment_id={commentId}, format=json
```

PC 分支走两级：

### ① sns 删评

```
POST https://sns.qzone.qq.com/cgi-bin/qzshare/cgi_qzsharedeletecomment?&g_tk={g_tk}
```

URL 里那个 `?&` 不是笔误，源码就是这么拼的（`client.ts:4326`）。参数是 `inCharset` / `outCharset` / `plat=qzone` / `source=ic` / `hostUin` / `uin` / `topicId={uin}_{tid}` / `feedsType=100` / `commentId` / `commentUin` / `format=fs` / `ref=feeds` / `paramstr='2'` / `qzreferrer`。`commentUin` 没传时默认填 `uin`。

这条能成的标志是 `ret === 0`，命中就包成 `{ code: 0, message, ...原始 }` 返回。非 0 不抛错，继续往下。

### ② taotao 兜底

```
POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delcomment_ugc?g_tk={g_tk}
body: hostuin={自己号}, uin={作者}, tid, comment_id, format=json, qzreferrer
```

注意这里 `hostuin` 是自己号，`uin` 是帖子作者，两个别填反。返回值直接 `parseJsonp` 出来，没做额外包装。

`sns` 那条在仓库里没有实测记录，我不知道它在当前环境成不成。taotao 这条也没单独标过，只知道 `compatibility-matrix.md` 把 `emotion_cgi_*` 系列整体评价为 POST 可用。两条都算未实测，删评论成功与否最好以日志里的 `ret` / `code` 为准。

## 转发（相邻功能）

放在这儿是因为它和评论共用一条降级通路，实现细节在 [emotion-api.md](emotion-api.md)，这里只说和本文相关的点：

```
POST .../taotao.qzone.qq.com/cgi-bin/emotion_cgi_forward_v6?g_tk={g_tk}
     body: tid, ouin, opuin, hostUin, topicId={ouin}_{tid}, con, feedversion=1, ver=1, code_version=1, appid=311, format=json, qzreferrer

↓ code !== 0 时

POST .../taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds?g_tk={g_tk}
     body: hostUin, topicId={ouin}_{tid}, content, forward=1, format=json, qzreferrer
```

`emotion_cgi_forward_v6` 在矩阵里标的是可能返回 `-3000`，所以降级不是可选优化，是常态。

## 这套东西为什么长这样

`CLAUDE.md` 里两句话把背景交代得很清楚：`get_like_list` 这类 PC 端点「often empty/500 in probes」，评论和详情「often 500/empty, mobile detail often 404」，所以数据源整体偏向 `feeds3_html_more`。

这解释了这个模块的形状——写操作还留在老 JSON 接口上（它们还能用），读操作全部重写成「拉 HTML 再解析」。代价是评论 id 变成了帖内序号、点赞列表可能和计数对不上，这些都是解析 HTML 换来的。

反爬和限流的公共处理见 [api-overview.md](api-overview.md)。撤销、退避、缓存的完整策略见 [fallback-strategy.md](fallback-strategy.md)。

## 对外暴露的动作

bridge 这一层把这些能力包成了 OneBot 动作，名字和参数取自 `src/bridge/actions.ts`，README 的动作表里也列了：

| 动作 | 参数 | 底层调用 |
|------|------|---------|
| `send_like` | `tid`、`user_id`（可选）、`abstime`、`appid`、`typeid`、`unikey`、`curkey` | `likeEmotion()` |
| `unlike` | 同上 | `unlikeEmotion()` |
| `send_comment` | `target_tid` 或 `tid`、`content`、`target_uin` 或 `user_id`、`reply_comment_id`、`reply_uin`、`appid`、`abstime` | `commentEmotion()` |
| `delete_comment` | `tid`、`comment_id`、`uin` 或 `user_id`、`comment_uin`（可选） | `deleteComment()` |
| `get_comment_list` | `user_id`、`tid` | `getCommentsBestEffort()` 的对象形式（拿回 `comments` / `availability` / `identity`） |
| `get_like_list` | `user_id`、`tid` | `getLikesBestEffort()`，返回 `list` / `likes` / `availability` / `identity` |
| `get_like_list_best_effort` | `user_id`、`tid` | README 里列了，但 `src/` 里找不到处理器，见下文 |
| `get_traffic_data` | `user_id`、`tid` | `getTrafficData()` |
| `forward_msg` | `user_id`、`tid`、`content` | `forwardEmotion()` |

动作名到处理函数的映射是 `action_${动作名里的点换成下划线}`（`src/bridge/actions.ts:251`），所以调 `get_like_list` 走的是 `action_get_like_list`（`actions.ts:663`）。顺便说明：那个函数调的是 `getLikesBestEffort()`，不是 `getLikeListBestEffort()`，两者差一层 SDK 包装，`availability` 字段只有前者有。

`user_id` 这类参数都允许留空，缺了会从 `postMetaCache` 按 tid 补。补不上时 `send_like` 仍然会把请求发出去（appid 兜底 311），`delete_comment` 则会直接返回「缺少 uin/user_id 且无法从缓存补全」，不会瞎删。

还有一个细节：`send_comment` 和 `delete_comment` 都会先过一遍 `resolveTidForComments()`（`client.ts:430`）——如果传进来的 tid 是纯数字（说明调用方拿时间戳当 tid 用了），它会去缓存里找 `abstime` 相同的帖子、换成真正的 tid。这个兜底是给上游传错参数用的，不是常规路径。

## 调用示例

直接调 client 的样子，QQ 号用仓库里那套合成号：

```typescript
// 点赞。tid 用 feeds3 解析结果里的原值，别自己拼
// 形态可能是 16 位以上 hex，也可能是 d{号}_{时间戳}_… 这种带下划线的复合串
const likeRes = await client.likeEmotion('1100000012', 'd1100000012_1750000000_', 1750000000, 311, 0);

// 取消点赞：参数完全一样，内部走另一条判断
await client.unlikeEmotion('1100000012', 'd1100000012_1750000000_', 1750000000, 311, 0);

// 发一级评论
await client.commentEmotion('1100000012', 'd1100000012_1750000000_', '路过看看');

// 回复某条评论：第 4、5 个参数是被回复评论的 id 和被回复者
await client.commentEmotion('1100000012', 'd1100000012_1750000000_', '同感', '1', '1100000011');

// 删评论
await client.deleteComment('1100000012', 'd1100000012_1750000000_', '1', '1100000011');

// 拿评论列表（实际是 feeds3 解析）：位置参数形式，评论在 commentlist 里
const comments = await client.getCommentsBestEffort('1100000012', 'd1100000012_1750000000_', 20, 0);

// 换成对象形式就变成 SDK 返回，评论在 data.comments 里，另有 availability
const sdkResult = await client.getCommentsBestEffort({ uin: '1100000012', tid: 'd1100000012_1750000000_', num: 20, pos: 0 });
```

判断成功别只看 `code`，源码里到处是 `code === 0 || ret === 0`，封装一层：

```typescript
function okOf(r: ApiResponse): boolean {
  return r['code'] === 0 || (r['ret'] as number) === 0;
}
```

注意这个写法对 `unlikeEmotion` 的方法 2 不成立——那里只判了 `code`（`client.ts:4184`）。虽然实践上很少碰到，但知道有这回事。

## 排错清单

按可能性从高到低：

1. 返回 `code = -10000`。这是限流，不是参数错。点赞和评论这类写操作碰到它，等一会儿再来，别连续重试。
2. 评论列表返回空数组但帖子明明有评论。看日志里是 `post_counts_present_but_not_embedded`（有计数没展开）还是 `html_no_matching_comment_bucket`（HTML 里有评论区但没匹配上）。前者是数据源限制，换不了；后者可能是解析器没跟上页面改版。
3. 删评论返回非 0。先确认 `comment_id` 是不是 feeds3 给的那个帖内序号——序号拿去删大概率删不掉。真删不掉就在网页端手工确认评论还在不在。
4. 点赞返回成功但刷新页面没赞上。检查 `unikey` 是不是合成的。非 311 的 appid 用 `/app/{tid}` 合成出来的 unikey 源码自注「通常不准」，得从 feeds3 HTML 里取真实值。
5. 回复评论没带上 @ 前缀。`commentEmotion` 会自动补 `@{uin:...,nick:...,auto:1}`，但前提是 `replyUin` 也传了。只传 `replyCommentId` 不传 `replyUin`，前缀不会加，服务端可能当成一条独立评论。

## 未实测 / 源码里没找到的部分

列清楚，别当成已验证：

1. `internal_dolike_app` 怎么区分点赞和取消——源码里两次请求的 `active` 都是 `'0'`，只有 `abstime` 有无之差。未实测，建议抓包确认。
2. `cgi_qzsharedeletecomment`（删评第一级）在实机的可用性。源码里没有任何探测或降级记录，未实测。
3. `emotion_cgi_delcomment_ugc` 在实机的可用性。同样未实测。
4. `emotion_cgi_re_feeds` 的两条通路哪条更常成功。代码是先 user 后 h5，但我没找到记录两者成功率的实测数据。
5. `QZONE_INSTANT_LIKE_SOURCE` 环境变量在 `src/` 里没有消费点，`routes.unlike` 也没有读取点。这两个目前是空转的。
6. 移动端 `like` / `del_comment` / `get_comment_list` 的实机表现只以 `compatibility-matrix.md` 的记录为准（404 / 不稳定），我没有重新跑过。
7. 点赞列表上限：feeds3 单次 `count=50`，但一页 HTML 能展开多少点赞人、有没有截断，源码里没写，我也没测出来。

## 和已有文档对不上的地方

发现四处，不动别人的文档，只在这里列出来：

1. [fallback-strategy.md](fallback-strategy.md) 写「取消点赞: **internal_dolike_app**: `active=0` 取消，最可靠」，而 `likeEmotion()` 点赞也用 `active=0`。两条说法冲突，源码里看不出区分依据，见上文「取消点赞」一节。
2. 项目根 `README.md` 的动作表把 `get_like_list_best_effort` 描述成「PC detail → Mobile detail → feeds3 HTML → 空数组」，还写着 `get_like_list`「依赖说说详情接口」。现在的源码里 `getLikeList()` 就是一个透传，直接调 `getLikeListBestEffort()`，全程只走 feeds3，没有任何 detail 依赖。README 这段应该是旧实现的残留。顺带一提，`get_like_list` 这个动作实际调的是 `getLikesBestEffort()`，和 README 写的「基础版」也不是一回事。
3. 同样是动作表里的 `get_like_list_best_effort`：整个仓库里除了 `README.md:265` 和这篇文档，`src/` 下搜不到这个动作名对应的处理器。按 `actions.ts:251` 的拼名规则它会请求到 `action_get_like_list_best_effort`，那个函数不存在，只会返回 1404。这条我列为未实测，只能说源码里没找到。
4. `routes.delete_comment` 的默认值是 `'pc'`，但 `probeApiRoutes()` 只探测 `detail` 和 `comments` 两个键，所以那个 `'mobile'` 分支实际上永远不会被自动切过去。要么是留作手工开关，要么是探针漏了这个键，源码里没有说明。
