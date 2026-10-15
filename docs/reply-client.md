# 评论回复：本地签名与无浏览器 token 引导

2026-10-15，已用用户提供的作品、父评论和设备参数完成真实 GET 验收。`X-Dynosaur`、`X-Gnarly` 每次由项目 `TiktokSigner` 在 Python 本地计算；`X-Bogus` 沿用项目值 `1`。没有复用抓包签名，没有启动浏览器、CDP 或 Node.js。

## 实测结果

| 用例 | 真实响应 | 结论 |
| --- | --- | --- |
| 原 token + 新签名 | HTTP 200，空正文，带新 `x-ms-token` | 不能只凭 HTTP 200 判成功 |
| 采纳响应 token，同步 query/Cookie，全部重签 | HTTP 200、`status_code=0`、3 条回复 | 恢复正确业务响应 |
| 清空输入 token 和全部输入 Cookie | 第一次 HTTP 403 空正文，下发 128 字符 token；第二次 HTTP 200、3 条回复 | 不依赖事先复制 token；正式客户端 3 次独立启动均成功 |
| 同一客户端从 cursor=6 翻页 | HTTP 200、3 条回复，返回 cursor=9 | 翻页验收通过 |
| 把 Dynosaur 改为 `invalid`，重新计算 Gnarly | 两次 HTTP 200 空正文 | 无效 Dynosaur 对照失败 |
| 对照后恢复正常本地签名器 | HTTP 200、3 条回复 | 正向基线仍有效 |
| `/web/report` 新 token + 新签名，含响应 token 轮换 | 两个独立申请会话，各 3 次 HTTP 200 空正文 | 报告签发路线仍未通过该接口 |

所有成功样本均核对 `aweme_id` 等于请求作品、`reply_id` 等于父评论，`cid` 非空且页内唯一；正式客户端还检查实际出站 query/Cookie 中的唯一 token 确实等于本次值。作品 `7654645616496168200`、父评论 `7655382457587958535`，初始 count=3、cursor=3，成功返回 cursor=6、has_more=1。逐请求脱敏记录见 [reply-client-evidence.json](reply-client-evidence.json)。

`msToken` 仍是服务端在线签发的不透明值，本次补齐的是不用浏览器的获取、轮换和重签闭环，没有宣称离线算出有效 token。128/148/152/176 是样本长度，不能作为通用有效性规则。成功 Dynosaur 长度为 512，也不应被旧样本的固定长度拒绝。

## 实现

- `api/comment_replies.py`：新增 `CommentRepliesClient`，接收精确请求上下文；丢弃旧签名、保留业务查询的顺序与转义，每次重新签名。
- `builder/signer.py`：新增显式 `allow_missing_ms_token`，仅放行这个 HTTPS GET 入口的首次引导请求；原有默认行为保持必需 token。
- `builder/auth.py`：处理响应头 `x-ms-token`，同步 Cookie、document cookie、localStorage、sessionStorage，防止旧 storage 把新 token 覆盖。
- `reply_verify.py`：可执行真实验证，只输出脱敏证据，成功退出码为 0。

客户端最多发送两次 GET：仅在第一次为 200/403 空正文且带不同的合法新 token 时，采纳响应 token 并重签一次。HTML、429、业务 JSON 错误及网络错误不会盲目重试。每次请求清理该 HTTP session 的隐式 Cookie jar，保证实际发送的 token 与签名上下文一致。传入的 session 应专供此客户端使用。

验证器按分号解析请求 Cookie，保留 `g_state={...}` 等 JSON 值；同时拒绝出站重复或被替换的 msToken。75 项本地测试通过，包含 token 轮换、存储同步、重试边界、错误脱敏、真实出站 token 和业务身份校验；另有 2 个现有 protobuf 弃用警告。

## 使用

在项目根目录运行。私有上下文 JSON 结构：

```json
{
  "url": "https://www.tiktok.com/api/comment/list/reply/",
  "headers": {"user-agent": "实际 UA", "referer": "实际视频页 URL"},
  "params": {"item_id": "作品 ID", "comment_id": "父评论 ID", "count": "3", "cursor": "3", "aid": "1988"},
  "cookies": {}
}
```

这是结构示意；`params` 应保留实际请求中的其他业务、设备、地区、语言参数，不能把这个缩减示例当作已验证的最小参数集。也支持把原始查询串直接放在 `url` 中。上下文和原始响应存入已忽略的 `.private-ms-token/`，不要提交凭证。

```sh
.venv/bin/python reply_verify.py \
  --context .private-ms-token/reply-context.json \
  --proxy http://127.0.0.1:7892 \
  --output .private-ms-token/reply-check.json
```

默认 `--token-source bootstrap`，忽略全部输入 token、Cookie 和签名。保留原会话并验证轮换可加 `--token-source context --keep-cookies`。`--token-source report` 用于复核报告路线，当前回复接口验收失败。代理按实际网络配置，不需要时省略。

业务调用返回完整回复 JSON，调用方自行处理和保护数据：

```python
import json
from pathlib import Path
from api.comment_replies import CommentRepliesClient

context = json.loads(Path(".private-ms-token/reply-context.json").read_text())
with CommentRepliesClient(context, proxy="http://127.0.0.1:7892",
                          bootstrap=True, keep_cookies=False) as client:
    first = client.get_replies()
    comments = first["data"]["comments"]
    if first["data"].get("has_more"):
        following = client.get_replies(cursor=first["data"]["cursor"])
```

本次验收要求返回非空回复；末页空列表会被视为未取得验收样本，不代表服务端业务报错。当前实现适用于已知有回复的目标和分页验证。

## 验证边界

设备 ID、UA、region、页面和其他业务参数仍来自用户提供的请求上下文；未实现从零生成全套浏览器身份，也未验证任意作品/地区。已有 `TiktokWebAPI.get_comment_replies` 的浏览器上下文与固定签名长度检查未在本次扩展；以上实测入口是新增 `CommentRepliesClient`。

本次坏 Dynosaur 对照能区分成功/失败，但不能据此推断每个签名字段、UA、query 哈希都被服务端逐项强校验。此前 Gnarly/Bogus 消融结果与限制见 [早期参数对照](reply-validation.md)。`/web/report` 返回 token 不等于回复接口接受，报告 token 的失败证据仍保留。结果适用于本次网络和服务端策略，不作跨接口或长期可用保证。

环境说明：本地 Python 3.12 已完成以上验证。原 `requirements.txt` 的 `protobuf>=3.20,<5` 与 `blackboxprotobuf==1.0.1` 依赖声明存在冲突，本次未改动该依赖组合，也未验收全新 Docker 构建。
