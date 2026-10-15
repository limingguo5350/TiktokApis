# TikTok msToken：本地编码、在线签发

此实现无需浏览器、CDP、Playwright 或 Node.js，也无需预先复制 msToken。Python 在本地构造设备报告、加密 `strData`，通过 HTTPS 请求 TikTok 服务端签发 token。**它需要联网，不是离线伪造服务端 token。**

## 使用

使用项目的 Python 3.11+ 环境及现有 requirements.txt。独立的匿名申请与刷新：

```python
from utils.mstoken import MsTokenClient

with MsTokenClient(proxy="http://127.0.0.1:7892") as client:
    token = client.get_token()
    cached = client.get_token()  # 默认 300 秒内复用本客户端的已接受结果
    renewed = client.refresh()  # 强制上报，带上前一个 token
    # 将 token/renewed 交给请求层使用；不建议打印或记录完整值。
```

代理可省略，取决于本机网络。CLI 会读取 `HTTPS_PROXY` / `https_proxy`，库可通过显式 `proxy` 指定。申请与业务请求应使用一致的 UA、代理出口及会话配置。

在项目根目录执行仅输出脱敏结果的检查：

```sh
python mstoken_demo.py --proxy http://127.0.0.1:7892 --refresh
```

成功示例：

```json
{"issued": true, "token_length": 152, "region": "sg", "refreshed": true, "refresh_length": 152, "rotated": true}
```

长度是本次观察值，不是有效性判据；服务端也可能续签同一值。`--region va` 可选择备用区域，但本次在线验证仅覆盖 `sg`。

## 接入 TiktokAuth

```python
from builder.auth import TiktokAuth

# 也可以 from_cookie(cookie, ...)，保留现有登录态及其他运行时字段。
auth = TiktokAuth()
try:
    auth.ensure_ms_token(proxy="http://127.0.0.1:7892")
    # 请求层可直接使用 auth.ms_token / auth.cookie_str。
    auth.refresh_ms_token()
finally:
    auth.close_ms_token_client()
```

构造 Auth 和读取 `ms_token` 属性均不会自动联网。已有外部 token 时，`ensure_ms_token()` 保留该值；`refresh_ms_token()` 强制申请新结果。由 Auth 获取的 token 使用其内部客户端缓存策略。若要更换内部客户端的代理、profile 或区域，先调用 `close_ms_token_client()`，再传新配置并显式刷新。

刷新成功会一起更新 cookie、`document_cookie`、localStorage 和 sessionStorage 中的 msToken，保留其他字段及原始 cookie 的重复项。协议或网络失败会抛出 `MsTokenError`，不接受错误响应附带的 token，不修改 Auth 里已接受的 token。不会回退到随机字符串。

也可传 `auth.ensure_ms_token(client=client)`。外部客户端须与 Auth 的 `user_agent` 一致，生命周期由调用者管理。每个客户端对应一个会话，不应在不同账号之间共享。传入自定义 HTTP `session` 时，需在该 session 自身配置代理；客户端不会替调用者关闭它。

300 秒是本地缓存策略，可通过 `cache_seconds` 修改；它不代表服务端过期时间。客户端不会后台定时刷新，调用 `get_token()` 时才判断缓存时间。

## 协议依据与实现

参考了 DouYin_Spider 的“本地报告 → 加密 → 服务端签发”流程，但 TikTok 的接口、报告和编码不同，未直接照搬 Douyin 的 RC4 编码或应用 ID。

本次 TikTok 页面 SDK 与本仓库 `signing/source/webmssdk_legacy.js` 内容一致：

- SCM 版本 `1.0.0.417`，报告 SDK 版本 `5.3.2`，应用 ID `1988`。
- 文件 SHA-256：`6625c4fd90012eb2b6166002b278ac99ba2a2495325590f8eda9071544a814c8`。
- 上报地址：`https://mssdk-sg.tiktok.com/web/report`。
- JSON 外层：`magic=538969122`、`version=1`、`dataType=8`、`strData`、动态 `tspFromClient`；HTTP content-type 为 `text/plain;charset=UTF-8`。
- 成功确认体为 `{"maigc":538051346,"version":1,"dataType":8}`，这里的 `maigc` 是真实协议拼写。
- 从 `x-ms-token` 读取 token，缺失时检查 `Set-Cookie`。HTTP 200、正确协议确认及有效 token 三项必须同时满足。包含非零 `code` 的响应会被拒绝。

`strData` 的 s1 编码使用 12 个随机 uint32、SDK 的自定义 ChaCha 变体、按和计算的密钥插入位置和专用 Base64 字母表。复用了 `signing/pure.py` 的加密原语；s1 字母表中的 `+` 与项目另一个算法字母表中的 `-` 有区别。

SDK 支持压缩与未压缩两种分支。运行时实现选择已在线验收的未压缩分支（标记 `0x4b`），因此没有为专有压缩器增加依赖。低层 `encode_strdata(compressed=True)` 仅接受已经压缩的字节，用于真实样本对照，不执行压缩。

`utils/mstoken_profile.json` 是脱敏后的固定设备模板，包含屏幕、WebGL 等设备属性；它不探测当前机器，也不包含抓取的 token、账号 cookie 或 IP。请求时重新构造时间戳、序号、tokenList、随机密钥以及 UA 字段。通过 `profile=dict` 或 CLI `--profile file.json` 可提供完整的自有配置。仅替换 UA 不会自动重建设备模板中的全部 WebGL、字体和屏幕数据；业务请求需要由调用者保证这些字段一致。

## 验证结果（2026-10-15）

- 两份真实 SDK 报告的编码逐字节相同，原始明文分别为 6403、6273 字符；完整材料仅保留在 Git/Docker 忽略的私有目录。
- 公开测试包含两份 SDK 对合成明文生成的固定向量，不需要浏览器抓包文件即可运行。
- `python -m pytest -q tests`：42 项通过（包含真实请求验收器的边界测试），覆盖编码、随机性、缓存到期、刷新旧值传递、错误协议/HTTP/非 JSON/缺 token、会话隔离、Auth 同步和客户端关闭。
- 两个新建纯 Python HTTP 会话分别用默认客户端 UA 和 Auth 默认 UA，均取得 token，缓存复用正确，刷新后 token 发生变化。本次长度均为 152。独立 CLI 申请及刷新也通过。
- 使用新签发 token 和项目现有 Python 签名器读取 `/api/user/detail/`，收到 HTTP 200、0 字节响应体，**业务读取未通过**。请求未提供真实浏览器设备身份，不能据此单独归因于 token 或某一个签名参数。

脱敏结果见 `docs/mstoken-evidence.json`。上述签发验证不替代账号登录、设备身份、verifyFp、CSRF、ticket-guard 或各接口签名要求；现有 API 对这些字段的检查仍然适用。SDK/服务端协议变更时，应以新的真实样本更新实现及对照测试。


## 真实业务请求验证补充（2026-10-15）

这次没有只验证报告接口：先用独立匿名浏览器建立真实接口对照，再在 Python 中新申请 token，用项目 Python 签名器重新生成签名并发送真实业务 GET 请求。使用的目标为公开账号 `@tiktok`，没有登录。

| 接口 | 新 token 请求结果 | 返回业务字段 |
| --- | --- | --- |
| `/api/user/playlist/` | HTTP 200，业务码 0，9176 字节 JSON | `playList` 5 项，含 `name`、`mixName`、`videoCount`、`id` 等 |
| `/api/user/collection_list/` | HTTP 200，业务码 0 | `collectionList` 1 项，含 `name`、`total`、`collectionId` 等 |

两个请求都核对了实际发送 URL 中的 **唯一 msToken 等于本次申请值**，没有复用旧签名。样例数据为播放列表 `Songs Of The Summer 2026`（3 个作品），收藏合集 `Summer of Sports`。精确字节数及全部对照见 `mstoken-evidence.json`。

但反向对照也成功：相同匿名上下文中，使用本地随机假 token，或者去掉全部签名与 token，仍能拿到上述数据。另一次仅保留 msToken cookie、移除其他 cookie 的请求也返回了数据。这意味着：

- **已证实**：本次申请的 token 能被携带到真实请求中，并与 Python 签名器一起取得这两个公开接口的数据。
- **未证实**：服务端在这些响应中严格验证了 token；当前对照样本没有提供这种判别能力，不能把有数据返回等同于 token 通过全部风控。
- **未通过**：从零纯 HTTP 获取完整业务上下文。新建 HTTP 会话读取个人页收到 200 / 1462 字节内容，缺少页面初始化 JSON，未能继续建立完整上下文。
- **仍有失败**：`/api/user/detail/`、`/api/post/item_list/` 的对照存在空响应；浏览器自己也会出现此结果，不能单独归因于新 token。

网页 SDK 会轮换内部 token。向浏览器 storage/cookie 写入 Python token 后，观察到实际出站值被替换；这种请求已排除出成功证据。

### 复现业务验证

`mstoken_verify.py` 全程使用 Python，不启动浏览器。它接收一份私有请求上下文 JSON，结构为 `{"url":"https://www.tiktok.com/api/user/playlist/?...","headers":{...}}`，也支持 `/api/user/collection_list/`。URL 中应包含目标账号和同一匿名会话的实际设备参数；headers 至少含 `user-agent`、`referer`，按需带 cookie。上下文文件需要使用自己的新鲜匿名请求，不要提交版本库。

```sh
python mstoken_verify.py \
  --context .private-ms-token/playlist-context.json \
  --proxy http://127.0.0.1:7892
```

脚本依次运行新申请 token、假 token、不带 token/签名三个请求。它保留原始查询串的次序与转义，删除旧签名，并对带 token 的请求重新签名。请求之间隔离响应 cookie 变化；只输出业务码、数据条数和公开样例字段，不输出 token/cookie/完整 URL。只有 HTTP 200、JSON、业务码均为 0、目标数据非空且实际 token 匹配时，`business_success` 才为 true。空列表在此验收场景中视为未取得预期数据，不等同于接口本身报错。

当前线上结果同时包含：

```json
{
  "issued_token_business_success": true,
  "controls_also_return_data": true,
  "strict_token_validation": "not_established"
}
```

退出码 0 仅表示新 token 请求取得预期业务数据；严格校验结论必须另看对照结果。验收脚本不提供账号登录或设备身份生成，也不保证未来服务端策略不变。

## 评论回复接口的更严格对照（2026-10-15）

用户提供的 `/api/comment/list/reply/` 请求在保留原 token、仅重算签名后，确实返回了 3 条对应作品和父评论的回复。换成本实现新签发的 token 并重签后，该接口返回 HTTP 200 空体，故新 token 尚未通过此接口的业务验收。首轮对照发现 query token 可省略（Cookie token 保留），但把 query token 改坏会返回 403；Cookie token 可删（query token 保留）。不能把这两个结果合并为“两处都不需要 token”。完整对照及后续基线失效的限制见 `reply-validation.md` 和 `reply-validation-evidence.json`。


## 评论回复已通过的无浏览器获取路径（2026-10-15 后续）

后续发现回复接口自身的响应头 `x-ms-token` 可用于 token 引导与轮换。新增 `CommentRepliesClient` 在清空输入 token 和全部 Cookie 后，首个签名 GET 获得 403 与新 token，同步 query/Cookie 并重签后取得 3 条身份匹配的回复；3 次正式客户端独立启动、翻页、坏 Dynosaur 反向对照与恢复正向均已验证。原 token 空响应同样通过采纳响应 token 恢复。

该路径无需浏览器/CDP；业务设备参数仍用给定上下文。`MsTokenClient` 的 `/web/report` 路线在此接口仍为空体，未被本次成功结论替代。`TiktokAuth.apply_set_cookie` 现会处理响应头轮换并同步 storage，防止旧值覆盖。完整代码入口、命令和证据见 [reply-client.md](reply-client.md) 与 [reply-client-evidence.json](reply-client-evidence.json)。
