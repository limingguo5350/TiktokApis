# TikTok 评论回复接口：参数校验实测

> 后续实测已恢复成功基线，并实现无输入 token/Cookie 的纯 HTTP 回复读取。最新代码、复现方法和验证边界见 [reply-client.md](reply-client.md)；本文件保留早期消融与当时失败结果。

测试日期：2026-10-15，Asia/Shanghai。目标为用户提供的 `GET https://www.tiktok.com/api/comment/list/reply/`。本次只读取公开评论回复，没有登录、发布或执行互动。

**本次最明确的判别来自 `X-Dynosaur`：删除或替换成 `invalid` 后失败。query 中的 msToken 可以省略，但传入损坏值会返回 403。`X-Gnarly`、`X-Bogus` 和全部 Cookie 的移除没有导致首轮读取失败。** 这些是该时间、该接口、该请求上下文下的观察，不代表其他接口或所有风控场景。

脱敏逐请求结果见 `reply-validation-evidence.json`。原始 Cookie、token、签名和响应正文只保存在 Git/Docker 忽略的私有目录。

## 成功标准与基线

用户原样请求通过 requests 和 curl_cffi 各重放一次，两次均为 HTTP 200、0 字节响应体，不能计为成功。

保留用户的 token、Cookie、业务参数、请求头，仅用项目现有 Python 签名器重新生成签名后，取得：

```json
{"status_code": 0, "comments_count": 3, "cursor": 6, "has_more": 1}
```

3 条回复的 `aweme_id` 都等于目标作品 `7654645616496168200`，`reply_id` 都等于目标父评论 `7655382457587958535`，各条 `cid` 不重复。因此验证了对应目标数据，而非仅检查 HTTP 状态或某个非空 JSON。

成功响应包含 `comments`、`cursor`、`has_more`、`status_code`、`status_msg`、`total`、`extra`、`log_pb`。评论含 `cid`、`text`、`user`、`create_time`、`aweme_id`、`reply_id` 等字段。公开证据不包含评论内容及评论者资料。

## 首轮单变量对照

本轮共 24 次请求，包括 18 个变体和穿插的 6 次基线。6 次基线全部返回业务码 0、3 条匹配目标的回复。每次新建 HTTP session，响应的新 Cookie 不带入下一组；使用同一配置的代理，未测量实际出口 IP 是否恒定。

| 修改项 | 实际结果 | 可以得出的结论 |
| --- | --- | --- |
| 重新签名的完整基线，6 次 | HTTP 200；业务码 0；3 条回复 | 本轮对照成立 |
| 删除 `X-Dynosaur`，重算外层 `X-Gnarly` | HTTP 200，0 字节 | 当前请求对该字段缺失敏感 |
| `X-Dynosaur=invalid`，重算外层 `X-Gnarly` | HTTP 200，0 字节 | 当前请求对该字段明显损坏敏感 |
| 删除 `X-Gnarly` | HTTP 200；业务码 0；3 条回复 | 本轮未表现为必须字段 |
| `X-Gnarly=invalid` | HTTP 200；业务码 0；3 条回复 | 本轮未表现为严格校验该值 |
| 删除 `X-Bogus` | HTTP 200；业务码 0；3 条回复 | 本轮未表现为必须字段 |
| `X-Bogus=2`，原样本为 `1` | HTTP 200；业务码 0；3 条回复 | 不能认定服务端要求固定为 `1` |
| 删除 query 中 `msToken`，保留 Cookie token，重算外层签名 | HTTP 200；业务码 0；3 条回复 | query token 可省略；未单独排除 Cookie token 回退的可能 |
| query token 改 1 个字符，保持长度/尾部格式并重新签名 | HTTP 403，0 字节 | 传入坏 query token 会被拒绝；不是简单的“缺失必拒绝” |
| 只删除 Cookie 中 `msToken` | HTTP 200；业务码 0；3 条回复 | 有原 query token 时，Cookie token 不必携带 |
| 只把 Cookie token 改坏 1 个字符 | HTTP 200；业务码 0；3 条回复 | 本次结果对 Cookie token 损坏不敏感 |
| query/Cookie token 同时改坏并重新签名 | HTTP 403，0 字节 | 与坏 query token 的失败一致 |
| 删除全部 Cookie，保留原 query token | HTTP 200；业务码 0；3 条回复 | 本次不依赖 Cookie 头才能读取 |
| 分别删除 `ttwid`、`odin_tt`、`_waftokenid`、`tt_chain_token`、`x-web-secsdk-uid`、`tt_csrf_token` | 各次均为 HTTP 200；业务码 0；3 条回复 | 没有证据把这些 Cookie 列为该次公开 GET 的必需项 |

修改 Dynosaur 或 query token 时重新计算外层签名，是为了避免把外层输入不一致误判为被修改字段的独立校验。所有 query token 损坏对照都重签，且保留其他业务输入。

注意：query token 可删除、Cookie token 可删除，分别在另一个位置仍有原 token 时验证。**两处同时删除尚无成功对照，不能由上述两个结果推导出整个请求完全不需要 token。**

## 尚未建立的结论

首轮结束后，两次后续基线及一组“原 token / 删除 query token”复核都返回 HTTP 200 空体，共 4 次。后续依赖性消融因此未执行，不用已经失败的请求继续推断必需参数。原因尚未锁定，可能涉及时效、运行时签名状态、请求频率、网络或服务端策略，当前证据不能选择其中一种。

因此，本次只确认 Dynosaur 的删除/明显损坏会失败，**没有在线证明**其每个内部字段、时间戳窗口、query 哈希、UA 或设备指纹都被严格验证。计划中的同长度损坏、改 query 后保留原 Dynosaur、UA 对照、移除设备 ID 与业务 ID 的测试均未在成功基线上执行，不计为已完成。

`item_id` 和 `comment_id` 在本次用于选择作品与父评论，`count`/`cursor` 用于分页；没有对它们逐项缺失或修改后的服务端行为下结论。该样本 HTTP Referer 为视频页、query `referer`/`root_referer` 为站点首页，仍然成功，所以不能只凭本地参数模板要求两者必须相同。

## 本地签名输入与服务端校验必须区分

本地 `builder/signer.py` 和 `signing/pure.py` 显示：

1. Dynosaur 编码包含未签名业务 query 的哈希、UA 哈希、当前页面、时间及环境字段。
2. Gnarly 编码包含 `业务 query + X-Dynosaur + msToken` 的哈希，以及 body、UA 和运行时字段；本地实现把 `X-Bogus` 排除在该输入外。
3. 改 token、业务 query、顺序或 URL 转义后直接复用签名，可能造成签名输入不一致。测试应保持原始序列化并重新计算相关签名。

这说明本地如何生成签名，不能据此直接宣布服务端逐项强校验。上面的服务端结论仅采用真实成功/失败对照。

## 对新生成 msToken 的影响

另外一次真实请求中，用 `MsTokenClient` 通过 `/web/report` 新取得的 token 同步替换 query/Cookie、重新生成签名后，该回复接口返回 HTTP 200、0 字节；同阶段原 token 加新签名取得 3 条回复。因此，**当前新 token 方案尚未通过此回复接口验收**。

以前的播放列表/收藏合集虽用新 token 返回数据，但假 token、无 token/无签名也能成功，不能拿来替代本接口的 token 有效性证明。报告签发成功、可用于发送网络请求，与在这个接口取得目标回复数据是不同验收条件。

本次共保留 32 条受控 API GET 结果。21 个成功响应核对了作品与父评论身份；没有改动生成器或业务 API 的实现来掩盖失败。
