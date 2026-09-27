# API 总览

这份文档只讲调 QQ 空间 Web API 时的那套公共约定：域名怎么分、URL 长什么样、哪些参数是必需的、请求发出去之后怎么判断成功。单个接口的参数表放在各自的文档里，说说看 [emotion-api.md](emotion-api.md)，点赞和评论看 [social-api.md](social-api.md)，feeds3 那条 HTML 链路看 [feeds3-parser.md](feeds3-parser.md)。

内容全部来自仓库源码，主要是 `src/qzone/config/constants.ts`、`src/qzone/client.ts`、`src/qzone/requestLayer.ts`、`src/qzone/infra/errors.ts`、`src/qzone/infra/retry.ts`。源码里翻不到的接口，这里一个字都不会补。

## 域名体系

源码里以常量形式固定的域名只有五个，在 `src/qzone/config/constants.ts:103`：

| 常量 | 域名 | 用途 |
|------|------|------|
| `QZONE_DOMAINS.user` | `https://user.qzone.qq.com` | PC 端点统一入口，走 proxy 转发 |
| `QZONE_DOMAINS.mobile` | `https://mobile.qzone.qq.com` | 移动端端点，直连不转发 |
| `QZONE_DOMAINS.upload` | `https://up.qzone.qq.com` | 图片上传 |
| `QZONE_DOMAINS.qzs` | `https://qzs.qzone.qq.com` | 登录成功页、qzonetoken 来源，也是 PC 请求的默认 Referer |
| `QZONE_DOMAINS.xlogin` | `https://xui.ptlogin2.qq.com` | 扫码登录预热 |

但实际请求打到的后端域名远不止这五个。PC 端用的是代理写法：真实后端域名被塞进路径里，外面套一层 `user.qzone.qq.com`。举几条源码里的实际 URL：

```
https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6
https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more
https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app
https://user.qzone.qq.com/proxy/domain/r.qzone.qq.com/cgi-bin/user/cgi_personal_card
```

所以看一条 PC 端点，要同时看两件事：外层的 `user.qzone.qq.com/proxy/domain/`，以及路径里的真实域名。`constants.ts:115` 那份 `API_PATHS` 把这些路径截好了，但 `client.ts` 里还有不少接口是直接拼的完整 URL，没走常量表。

按后端域名归类，源码里出现过的有：

| 真实域名 | 出现位置 | 干什么 |
|----------|----------|--------|
| `taotao.qzone.qq.com` | client.ts 多处 | 说说列表/详情/发布/删除/转发/评论/删评 |
| `ic2.qzone.qq.com` | client.ts:2424、client.ts:2542 | feeds3 HTML、feeds_html_act_all 动态流 |
| `w.qzone.qq.com` | client.ts:4124 | 点赞内部接口 internal_dolike_app |
| `r.qzone.qq.com` | client.ts:4365、4378、4552 | 用户资料、好友列表、流量数据 |
| `g.qzone.qq.com` | client.ts:4530 | 访客列表 |
| `photo.qzone.qq.com` | client.ts:4752 起 | 相册与照片，实机 500，见 [photo-api.md](photo-api.md) |
| `sns.qzone.qq.com` | client.ts:4326 | 删除评论 |
| `h5.qzone.qq.com` | client.ts:4260 | 评论的 h5 兜底通路 |

另外两条不走 proxy 的：`https://mobile.qzone.qq.com/...` 直连，`https://r.qzone.qq.com/fcg-bin/cgi_get_portrait.fcg` 也是直连（头像昵称，返回值要按 GBK 解，见 client.ts:4643）。

`up.qzone.qq.com` 只有一件事：图片上传。端点是 `https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={g_tk}`（`client.ts:3954`），POST，把图片 base64 放在 `picfile` 里，同时把 `skey` / `p_skey` / `p_uin` 也当普通参数带上。参数里还有个 `backUrls` 指向 `http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image`，是备用上传域。详见 [photo-api.md](photo-api.md)。

图片 CDN 单独说一句。`client.ts:782` 注释写得很直白：`photo.store.qq.com` 用 HTTP 请求经常回 502，所以代码统一把 http 改写成 https。用户图片的实际放行域名是 `qpic.cn` 和 `photo.store.qq.com`（`src/qzone/feeds3/comments.ts:139`），而 `qzonestyle.gtimg.cn`、`qlogo.cn`、`qzapp.qlogo.cn`、`/ac/b.gif` 这些是表情、头像、埋点，提取用户图时会被显式排除（`comments.ts:119`）。feeds3 请求参数里的 `sidomain=qzonestyle.gtimg.cn` 也是静态资源域，不是数据域。

## 基础 URL 与 PC / Mobile 的分界

仓库文档的约定是：PC 端指 `user.qzone.qq.com/proxy/domain/` 系列，Mobile 端指 `mobile.qzone.qq.com` 系列（见 [README.md](README.md) 的约定小节）。这条不只是叫法区别，两边的行为差得挺多。

| 对比项 | PC 端 | Mobile 端 |
|--------|-------|-----------|
| 基础 URL | `user.qzone.qq.com/proxy/domain/...` | `mobile.qzone.qq.com/...` |
| 响应格式 | 多数是 JSONP，要剥壳 | JSON |
| g_tk | 需要 | 需要（拼在 query 上） |
| 请求头 | 桌面 UA + 一堆 Sec-Fetch-* | 移动 UA + `X-Requested-With` |
| 实机成功率 | POST 高，GET 看接口 | 普遍偏低，多为 404 / 空体 |

`compatibility-matrix.md` 给移动端判的词更重：`mobile.qzone.qq.com/like` 实机 404，`get_mood_list` 404，`detail` 0 字节。所以源码里移动端基本只当兜底，不是主路径。

## 通用参数

下面这些参数在多个接口里反复出现，含义先说清楚，后面各接口文档就不再重复解释。

`g_tk` 是 CSRF 令牌，几乎所有端点都要带。GET 夹在 query 里，POST 也一样拼在 URL 的 query 上，不在 form body 里，源码就是这么写的。算法是对 `p_skey` 做 hash33，拿不到 `p_skey` 就退到 `skey`，两个都没有就返回 5381（`client.ts:964`）。算法本身和 Cookie 体系在 [auth.md](auth.md) 里有完整代码。

`qzonetoken` 是页面里嵌的服务端令牌。它只在少数场景需要——源码注释明确写了「qzonetoken 在所有 API 调用中均为可选参数」（`client.ts:2237`），而且为了不弹浏览器，HTTP 抓不到就冻结 10 分钟不再试。当前用到它的地方主要是 `emotion_cgi_getdetailv6` 的几个参数变体。抓取地址和正则见 [auth.md](auth.md)。

`uin` 有两种含义，别混。作为请求参数时它通常是「目标用户 QQ 号」，但 `feeds_html_act_all` 里的 `uin` 是页面语境参数，真正决定返回谁的数据是 `hostuin`（`client.ts:2465` 那段注释专门澄清了这点）。另外 Cookie 里的 `uin` 带 `o` 前缀（`o1100000011`），和参数里的裸 QQ 号不是一回事。

`format` 取 `json` 时请求 JSON/JSONP，取 `fs` 或 `jsonhtml` 时返回 HTML 片段包裹的 JSONP。评论的 h5 兜底通路就用 `format=fs`（`client.ts:4272`），图片上传用 `output_type=jsonhtml`（`client.ts:3934`）。想看纯 JSON 的接口可能拿不到，这是接口本身的差别，不是写法问题。

`qzreferrer` 源码里统一由 `getQzreferrer()` 生成，值就是 `https://user.qzone.qq.com/{当前登录号}/main`（`client.ts:973`）。它同时出现在请求体和请求头里——PC 请求头的默认 Referer 是 `https://qzs.qzone.qq.com/`，而带 `qzreferrer` 的调用会把 Referer 换成 `.../main`。

`appid` 分两套，容易搞混：

- 登录侧的 `appid=549000912&daid=5`，出现在 xlogin 预热 URL 里（`client.ts:1220`），这是 QQ 空间 Web 登录页的标识，跟业务数据无关。
- 说说/feed 侧的 `appid`，默认 `311`（`client.ts:4118` 等多处 `if (!appid) appid = 311`）。`202` 和 `2100` 是音乐分享（[feeds3-parser.md](feeds3-parser.md) 有专门一节），`217` 是「点赞记录」这类非用户发布的动态，个人流里会混进来，源码默认把它过滤掉（`client.ts:2944`）。

`typeid` 跟着 appid 走，说说默认为 0；构建 unikey 时只有 `appid=311` 才会走 `/mood/{tid}` 路径，其它 appid 落到 `/app/{tid}` 且源码自注「通常不准」（`client.ts:4094`）。所以其它 app 类型的帖要传从 feeds3 HTML 里扒出来的真实 unikey。

`abstime` 是发布时间戳，点赞和评论都要。传 0 时 `likeEmotion` 会从 `postMetaCache` 里按 tid 补全（`client.ts:4109`）。

`unikey` / `curkey` 是点赞的定位键。`unikey` 形态是 `http://user.qzone.qq.com/{作者号}/mood/{tid}`，`curkey` 一般是当前页面的 key，源码里缺省时直接取 `unikey` 自己（`client.ts:4120`）。这两个值从 feeds3 HTML 里抓最准，缓存里的 `likeUnikey` / `likeCurkey` 就是从那儿来的。

`fid`、`from`、`active`、`fupdate` 是点赞专用的：`fid` 就是 tid，`fupdate=1` 表示立即刷新计数。`active` 的语义有点乱，别照直觉猜：PC 的 `internal_dolike_app` 点赞和取消点赞都传 `active=0`（`client.ts:4129` 对 `client.ts:4172`），只有移动端才是 `active=0` 点赞、`active=1` 取消（`client.ts:4195`）。这个疑点写在 [social-api.md](social-api.md) 的取消点赞一节。另外取消点赞在 `like_cgi_likev6` 上换成了 `optype=1`（`client.ts:4181`）。

## GET / POST 与 JSONP

PC 端绝大多数写操作走 POST，`Content-Type: application/x-www-form-urlencoded`，参数是 form body。读操作走 GET，参数在 query 上。`post()` 会自动补 `Origin`（`client.ts:893`），规则按 URL 里的域名分：`user/taotao/sns` 打 `https://user.qzone.qq.com`，`up` 打 `https://up.qzone.qq.com`，`mobile/h5` 打 `https://mobile.qzone.qq.com`，其余落到 `https://qzs.qzone.qq.com`。

同一个接口 POST 通常比 GET 稳。`compatibility-matrix.md` 里 `emotion_cgi_getdetailv6` 和 `emotion_cgi_getcmtreply_v6` 都标了「GET 空 / POST 可用」，[fallback-strategy.md](fallback-strategy.md) 也把「POST 优先于 GET」写成容错原则之一。

JSONP 是这套 API 最烦人的地方。`parseJsonp()`（`src/qzone/utils.ts:17`）干了四件事，按顺序试：

1. 文本以 `{` 或 `[` 开头，当纯 JSON 解析；
2. 找 `frameElement.callback(...)`，这是 `emotion_cgi_publish_v6` 那种被 HTML 包起来的返回；
3. 找第一个 `(` 到匹配的 `)`，剥掉任意 callback 名；
4. 都不行再试一次 `JSON.parse`，失败返回 `{ _empty: true, raw: ... }`。

剥不出来不抛异常，返回一个带 `_empty` 标记的对象。这个设计贯穿整个请求层：失败不是异常，是一个能被识别的对象。`src/qzone/requestLayer.ts:41` 的 `parseRawResponse()` 就是这套判断的集中地——HTTP 状态、反爬特征、JSONP 剥壳、业务码提取、可选的 Zod 校验，最后拼成一个 `ParsedApiResult`。

判成功用 `isGenuineSuccess()`（`requestLayer.ts:115`），要求同时满足：没被识别为反爬、不是鉴权失败、不是限流、payload 不是空的、业务码要么不存在要么等于 0、Zod 校验通过。任何一个不过就是 false。

## 请求头与 Cookie

Cookie 是手动拼的。`request()` 在发请求前把内存里的 cookies map 拼成 `k=v; k=v` 塞进 `Cookie` 头（`client.ts:737`），响应回来再从 tough-cookie 的 jar 同步回 map。jar 里注入的域有三个：`.qq.com`、`.qzone.qq.com`、`.ptlogin2.qq.com`（`client.ts:684`）。换句话说，同一个 Cookie 值会被复制到三个域上，这不是精确的域隔离，是为了兼容不同端点的取 Cookie 行为。

两套头工厂：

- `pcHeaders()`（`client.ts:927`）：桌面 UA（从 8 个 UA 池里随机取）、`Accept` 偏 HTML、随机 `Accept-Language`、`Sec-Ch-Ua`、`Sec-Fetch-Dest: document`、`Sec-Fetch-Mode: navigate`、`Sec-Fetch-Site: same-site`，Referer 默认 `https://qzs.qzone.qq.com/`。
- `mobileHeaders()`（`client.ts:946`）：固定移动 UA、Referer `https://mobile.qzone.qq.com`、`Accept: application/json`、`X-Requested-With: XMLHttpRequest`。

UA 和 Accept-Language 随机化是为了降低指纹稳定性，池子在 `constants.ts:68` 和 `constants.ts:83`。拉图片的 `fetchImageWithAuth()` 另有一套头，`Referer` 固定 `https://user.qzone.qq.com/`，带 `Sec-Fetch-Dest: image`，还会显式带上完整 Cookie（`client.ts:786`）。

Cookie 持久化在 `src/qzone/cookieStore.ts`，格式是 `{ last_used, cookies }`，超过 14 天未使用直接删文件（`cookieStore.ts:15`）。拿不到 Cookie 就调用 `requireLogin()` 的接口会直接抛错，不会再往下走。

## 业务码

`code=0` 是成功，这条没有例外。其它码按 [compatibility-matrix.md](compatibility-matrix.md) 的实测记录抄过来：

| 业务码 | 含义 | 处理方式 |
|--------|------|---------|
| `0` | 成功 | 正常返回 |
| `-3` | 认证失败 / 权限不足 | 检查 Cookie |
| `-100` | 认证失败 | 检查 Cookie |
| `-3000` | 认证失败 / 接口限制 | 检查 Cookie；转发接口常见 |
| `-10000` | 限流（「使用人数过多」） | 降级到 feeds3 |
| `-10001` | 认证失败 | 检查 Cookie |
| `-10004` | 参数错误 / 接口已废弃 | 换替代接口 |
| `-10006` | 认证失败 | 检查 Cookie |
| `-4003` | 移动端业务受限 | 改走 PC |
| `-2` | 限流 | 退避重试 |

源码里把这些码分成了两个集合（`constants.ts:47`、`constants.ts:52`）：

```typescript
const AUTH_FAILURE_CODES = new Set([-3, -100, -3000, -10001, -10006]);
const RATE_LIMIT_CODES   = new Set([-10000, -2]);
```

`requestLayer.ts` 用它们给结果打 `isAuthFailure` / `isRateLimited` 两个标记，`client.ts:977` 的 `isAuthFailure()` 还会额外看 `subcode`。

HTTP 层同样不能只看状态码。`compatibility-matrix.md` 特意标了一条：HTTP 200 配空 body 是常见失败模式。`request()` 在 `200` 且 `data.length === 0` 时会打一条 `[empty-200]` 日志（`client.ts:756`），`parseRawResponse()` 则把 4xx/5xx 转成 `{ code: -httpStatus, _empty: true, http_status }`，401/403 记成鉴权失败，429 记成限流。

## 重试与限流

真正在 `client.ts` 主流程里生效的重试只有几处，都是写死的：

- 图片上传重试 2 次（`UPLOAD_IMAGE_RETRY_COUNT = 2`，`client.ts:62`）；
- 拉图片重试 3 次，退避 `250 * 2^attempt` 加随机抖动（`client.ts:831`）；
- 详情类接口全变体失败后进 5 分钟冷却，期间不再试 PC 路径，直接走移动端或列表兜底。

通用的 `withRetry()` 在 `src/qzone/infra/retry.ts:45`，默认 `maxRetries=2`、`baseDelayMs=1000`、`maxDelayMs=30000`、带 ±25% jitter，并且认得 `RateLimitError` 会用服务端给的 `retryAfterMs`。不过要说明白：这个函数目前只在 `src/qzone/index.ts:44` 对外导出，`client.ts` 里没有调用点。想要它生效得调用方自己包一层。

哪些错能重试，`src/qzone/infra/errors.ts:90` 写死了：`RateLimitError` 和 `NetworkError` 可重试，`AuthError` 和 `AntiCrawlError` 不重试。错误层级是 `QzoneError` 打底，往下分 `NetworkError`、`AuthError`（再往下 `SessionExpiredError`）、`RateLimitError`（`retryAfterMs` 默认 5000）、`AntiCrawlError`、`ApiBusinessError`、`ParseError`。

反爬识别在 `parseRawResponse()` 里做，扫的是原始文本，命中任一条就标 `isAntiCrawl`（`constants.ts:34`）：

```
系统繁忙 / 访问过于频繁 / 安全验证 / 请输入验证码 / tcaptcha / 需要登录 / 请先登录 / g_isSurvey / 标题是 QQ空间 且后面紧跟 script
```

被标成反爬的响应一律不算成功，也不重试。这时候多半是账号或 IP 被风控了，改重试参数没用。

限流除了业务码，还有一层应用级退避：[fallback-strategy.md](fallback-strategy.md) 记录认证失败后事件轮询器进 120 秒退避，评论和详情全失败各有 5 分钟冷却，qzonetoken 抓取失败冻结 10 分钟，Playwright 拉不起来冻结 30 分钟。缓存 TTL 都集中在 `constants.ts:8`（feeds3 页面缓存 30 秒、qzonetoken 成功缓存 5 分钟），feeds3 缓存上限 50 条 key。

## 请求模式小结

一条典型的 PC 读请求长这样，取自 `fetchFeeds3Html()`（`client.ts:2389`）：

```typescript
const params = new URLSearchParams({
  uin, scope: String(scope), view: '1', flag: '1', filter: 'all', applist: 'all',
  refresh: externparam ? '0' : '1',
  pagenum: epPagenum, begintime: epBasetime, dayspac: '5',
  sidomain: 'qzonestyle.gtimg.cn', useutf8: '1', outputhtmlfeed: '1',
  rd: String(Math.random()), usertime: String(Date.now()), windowId: String(Math.random()),
  g_tk: String(this.getGtk()), format: 'json',
});
```

几个模式值得记住：

- 随机数、时间戳、`windowId` 这些看起来没用的参数是照着浏览器抓包抄的，少一个就可能被判定为非浏览器请求。
- 翻页靠服务端返回的 cursor（`externparam`）反推下一页的 `pagenum` 和 `begintime`，不是自己算 offset。
- `g_tk` 每个请求都要重新算一遍。

POST 侧的例子见 `likeEmotion()`（`client.ts:4124`），参数是 `URLSearchParams` 直接当 form body，`g_tk` 拼在 URL 上，`format=json`，`qzreferrer` 跟 URL 上的 `.../main` 保持一致。

## 登录态是怎么进来的

Cookie 不会自己长出来，源码里有三条入口：

- `loginWithCookieString()`（`client.ts:1153`）：直接注入一整串 Cookie，最省事也最稳。要求至少含 `uin` / `p_skey` / `skey` / `p_uin` 之一，登完会请求空间首页验证一次。
- `loginWithPlaywright()`（`client.ts:1201`）：开一个 400×320 的浏览器窗口，先访问 `xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=549000912&daid=5&style=40&target=self...` 预热，再走页面里的二维码。协议版的 `ptqrlogin` 已经被弃用（容易被 403），细节见 [auth.md](auth.md)。
- 缓存文件：`cookieStore.ts` 从磁盘读回上次的 Cookie，格式 `{ last_used, cookies }`。超过 14 天直接删。

拿到之后还有两个自动行为要知道。一是 `syncCookieToEnvFile()`（`client.ts:649`）会把最新 Cookie 写回 `.env` 的 `QZONE_COOKIE` 和 `QZONE_COOKIE_STRING` 两行，所以进程重启不用手工更新；二是 `loggedIn` 只看 Cookie 里有没有号，不代表 Cookie 还有效（`client.ts:549`）。判断有效性得靠实际请求的业务码，或者 `validateSession()` 那套探测。

三种登录方式的取舍在 [auth.md](auth.md) 里展开过，这里只强调一句：所有接口调用前都会走 `requireLogin()`，未登录直接抛 `未登录`，不会静默返回空。

## 响应对象长什么样

`ApiResponse`（`src/qzone/types.ts:23`）是个很松的结构：

```typescript
interface ApiResponse {
  code?: number;
  message?: string;
  msg?: string;
  subcode?: number;
  http_status?: number;
  _empty?: boolean;
  raw?: string;
  [key: string]: unknown;
}
```

实际用的时候记住几个约定：

- 带下划线的都是本仓库自己加的，不是腾讯返回的。`_empty` 表示没解析出对象，`raw` 是被截断的原始文本（一般 200~500 字符），`http_status` 是 HTTP 层错误时补的。
- HTTP 层出错时构造出的对象是 `{ code: -httpStatus, _empty: true, http_status }`，也就是说 `code` 会是 `-404`、`-500` 这种负数，和业务码混在同一个字段里。判错的时候要能把这两种情况分开。
- `code` 和 `ret` 是两个不同的字段名，源码里经常两个都判（`code === 0 || ret === 0`）。看日志时别只盯一个。
- 反爬页面被识别时不会丢信息，`rawLogger` 会把原始响应落盘到 `cachePath` 下，文件名带 API 标签和触发原因（`src/qzone/rawLogger.ts:50`）。

## 校验层

跑在解析之后的是 Zod 校验。`src/qzone/schemas.ts` 一共定义了 14 个 schema，其中 10 个挂进 `SCHEMA_REGISTRY`（`schemas.ts:124`），剩下 4 个是子结构（表情、说说条目、评论条目、好友条目）不单独注册：

```
emotion_list / comment_list / user_info / friend_list / album_list
visitor / publish / social_action / shuoshuo_detail / traffic_data
```

调用方式是 `validateApiResponse(schemaName, payload, rawText)`，不通过不会抛异常，返回 `{ ok: false, data: null, issues, raw }` 并顺手把原始响应落盘（`src/qzone/validate.ts:27`）；核心路径上可以用 `validateOrThrow()` 改成直接抛。没注册的 schemaName 只打一条警告然后放行，不报错。

要注意校验只在显式传了 `schemaName` 时才跑（`requestParsed()` 的可选参数），不是默认全开。想确认某个接口在校验哪一层失败，看 `[validate:{名字}]` 开头的日志。

## 通用参数速查

同名前缀的字段散在各处，集中列一遍，省得来回翻：

| 参数 | 出现在 | 含义 |
|------|--------|------|
| `uin` | 绝大多数接口 | 目标用户 QQ 号；`feeds_html_act_all` 里是页面语境，看 `hostuin` |
| `hostuin` / `hostUin` | feeds 类、评论类 | 数据归属方，大小写两种写法都出现过 |
| `opuin` | 点赞类 | 操作者（自己） |
| `ouin` | 点赞、转发 | 目标帖作者 |
| `g_tk` | 全部写操作 | CSRF 令牌，query 上 |
| `qzonetoken` | 详情类部分变体 | 可选，见 auth.md |
| `qzreferrer` | 写操作 | `https://user.qzone.qq.com/{自己号}/main` |
| `format` | 全部 | `json` / `fs` / `jsonhtml` |
| `appid` | 说说、点赞 | `311` 默认，`202`/`2100` 音乐，`217` 噪音 |
| `typeid` | 说说、点赞 | 子类型，默认 0 |
| `tid` | PC 侧 | 说说 id |
| `cellid` | 移动侧 | 同一个东西，换了名字 |
| `fid` | 点赞类 | 也是 tid |
| `topicId` | 评论、转发 | `{作者}_{tid}`，h5 通路再加 `__1` |
| `abstime` | 点赞 | 发布时间戳 |
| `unikey` / `curkey` | 点赞 | `http://user.qzone.qq.com/{作者}/mood/{tid}` |
| `pos` / `num` | 分页 | 起始位置与条数 |
| `count` | feeds 类 | 单页条数，源码里 `feeds_html_act_all` 夹到 1~50（`client.ts:2505`）；`LIMITS` 里另有默认 20 的约定（`constants.ts:144`） |

`tid` / `cellid` / `fid` 是同一个东西的三个叫法，跨端拼参数的时候最容易在这儿翻车。

## 排查建议

遇到请求不通，按这个顺序看：

1. 有没有 `[empty-200]` 日志——那就是被限流或风控了，不是参数问题；
2. 业务码是不是 `-10000`——是就换 feeds3 那条 HTML 链路；
3. 业务码在 `AUTH_FAILURE_CODES` 里——Cookie 过期，重新登录；
4. 命中了反爬特征——停一会儿，别继续打。

具体的排障案例记在 [排障-Cookie失效与取不到说说.md](排障-Cookie失效与取不到说说.md) 里。
