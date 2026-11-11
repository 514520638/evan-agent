# 博客 Agent API（本地单进程版）

## 启动与访问

在 Agent 项目根目录运行：

```bash
uv sync
# 仅首次需要设置主人密码；已有账号不要重复初始化
uv run evan-auth init
uv run evan-api
```

API 监听 `http://127.0.0.1:8001`，接口说明在 `/agent-api/docs`。
模型 Key、MODEL、MiniMax 地址仍从 Agent 的 `.env` 读取，浏览器不保存模型 Key。
认证和聊天服务共用现有本地数据，但 Cookie 登录与终端登录相互独立。
只有聊天请求才初始化模型；接口启动、登录、健康检查不调用 MiniMax。

博客项目 `evanlink-cn` 是 React + Vite，本地 `npm start` 默认端口 8081。
已增加 `/agent-api` → `http://127.0.0.1:8001` 的 Vite 代理，保留原 `/api`
到博客后端 8080 的代理。JS/TS 两份现有 Vite 配置均已同步。
博客调用模块：`src/api/agent.ts`。本次不包含聊天窗口组件。

## 认证边界

- 所有非 GET 请求须带 `X-Agent-Request: 1`，浏览器来源须在白名单内。
- `AGENT_ALLOWED_ORIGINS` 为逗号分隔的完整 Origin，不支持 `*`。
- Cookie 为 HttpOnly、SameSite=Lax，路径 `/agent-api`；本地默认 Secure=false。
- HTTPS 部署时设置 `AGENT_COOKIE_SECURE=true`。优先采用博客同源路径代理；
  不同站点的跨站 Cookie 方案未在本版支持。
- 会话由服务端签发随机令牌，持久化保存令牌哈希，不接受前端自行提交 owner_id/role。
- 本人有效期 30 分钟，访客 1 小时。登录成功轮换 Cookie；旧身份聊天及审批清理。
- 退出登录会撤销服务端身份，旧 Cookie 不能恢复访问。不存在公开注册主人接口。
- 博客已有管理员登录与 Agent 本人认证是两套独立认证，不自动共享权限。
- 登录失败返回 401；认证存储沿用现有五次失败锁定规则。

当前所有会话、历史、待审批 checkpoint 使用本地 SQLite；只能单 worker 运行。
未过期 Cookie 在重启后有效；过期需重新登录，本人历史仍可恢复。未确认写入不自动执行。

## 接口清单

以下路径均以 `/agent-api` 开头。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 无凭据健康检查 |
| POST | `/auth/guest` | 创建访客 Cookie 会话；重复调用会撤销当前身份 |
| POST | `/auth/login` | `{ "password": "…" }` 本人登录 |
| GET | `/auth/me` | 查询当前 Cookie 身份 |
| POST | `/auth/logout` | 撤销身份并删除 Cookie |
| POST | `/conversations` | 新建聊天，返回 conversation_id |
| GET | `/conversations` | 列出当前身份的聊天 |
| GET | `/conversations/{id}` | 返回可见聊天文本和 pending 审批信息 |
| DELETE | `/conversations/{id}/messages` | 清空聊天及其待审批事项；执行中返回 409 |
| POST | `/conversations/{id}/messages` | `{ "message": "…" }` 发送消息，响应 SSE |
| POST | `/conversations/{id}/approvals/{approval_id}` | 提交审批，响应 SSE |
| DELETE | `/conversations/{id}/approvals/{approval_id}` | 取消本次审批，不恢复待执行工具 |

聊天接口只接受文本消息，不支持 `/login`、`/profile` 等终端命令。
资料查询和对话写入工具可以使用，完整的资料 CRUD HTTP 接口暂未添加。
无会话返回 401、访问其他身份聊天返回 403、审批过期／重复或聊天忙返回 409、
参数错误返回 400/422、限流返回 429。SSE 启动后的错误通过 error 事件返回。

## SSE 事件

事件格式为 `event: 类型` + `data: JSON`，空行分隔。

| type | 关键字段 | 页面处理 |
| --- | --- | --- |
| message_start | — | 开始一条助手消息 |
| delta | text | 将文本追加到当前助手消息，形成打字机效果 |
| done | answer | 将最后一条助手消息与最终答案对齐，结束等待 |
| approval_required | approval_id、action_requests、review_configs | 展示审批表单；本次 HTTP 流到此结束 |
| error | code、message | 提示本轮未完成，刷新聊天状态确认是否存在待审批事项 |

工具参数与 thinking 不作为 delta 输出，摘要模型输出也不显示。
部分工具调用前的说明文字仍可能作为助手消息输出。网络中断时不能撤回已显示
文字；未完成的聊天轮次不提交历史，但之前已确认执行的写入可能已经生效。
前端收到 EOF 却没有 done/approval_required/error，视为不完整流，不应盲目重发写入。

## 人工确认

1. 本人发送“记住，我在北京工作”。
2. 模型提出写入后，HITL 暂停，接口返回 approval_required，不占着连接等待确认。
3. 页面可展示公司、职位、行业、薪酬、待遇补充输入，将它们组合进最终 content。
4. 页面明确展示整条资料的 visibility、status 和最终原文，用户确认后提交 decisions。
5. 接口校验身份、会话、审批 ID、字段白名单，再恢复原 checkpoint。

示例：

```json
{
  "decisions": [
    {
      "type": "edit",
      "changes": {
        "content": "我在北京工作。\n公司：示例公司\n职位：开发工程师",
        "visibility": "private",
        "status": "published"
      }
    }
  ]
}
```

`approve` 原样批准，不附 changes；`edit` 只允许 content、visibility、status，
新增还可修改 category；`reject` 拒绝并让模型继续解释。不得更改工具名、
entry_id 或 owner_id。一批有多个 action_requests 时，decisions 必须同序且数量一致。
每次新的中断会产生新的 approval_id；成功恢复或取消后不能再次提交旧 ID。
页面刷新可通过 GET 聊天详情恢复 pending 表单。

本地终端仍使用 `MemoryReviewer`；HTTP 不调用它，不出现 `input()` 等待。
UI 上的 optional 补充项是前端表单职责，后端接受最后确认的 content。

## React 调用示例

```ts
import { agentApi, AgentApiError } from '@/api/agent'

try {
  await agentApi.me()
} catch (error) {
  if (error instanceof AgentApiError && error.status === 401) await agentApi.guest()
  else throw error
}
const { conversation_id } = await agentApi.createConversation()
await agentApi.send(conversation_id, '介绍一下你的项目经历', event => {
  // 在 React 状态中处理 event，禁止将模型文本直接作为 HTML 注入 DOM。
  console.log(event)
})
```

不要在每条消息前调用 guest()，否则会话将被重建。
登录／退出后丢弃旧 conversation_id 并重新加载列表或创建聊天。
使用 AbortController 可停止前端接收；中断后刷新聊天详情，不假定数据库操作回滚。

## 当前本地限制与后续部署

默认全局最多同时执行 2 个模型请求；每个来源每分钟 30 个聊天／审批请求、
20 次访客会话创建、10 次登录尝试；每个登录会话最多 20 段聊天；请求消息最多
12000 字符。限流与会话均为单进程，重启重置。当前按连接来源 IP 限流，
不信任用户自填 X-Forwarded-For；Nginx 代理后应在可信网关配置真实客户端限流，
否则本进程会将代理后的请求视作同一来源。

这次没有连接腾讯云或修改线上 Nginx。正式上线还需持久化聊天／审批、制定
多实例状态方案、设置生产 Origin 与 Secure Cookie、数据库备份和网关限流。
HTTPS 代理必须关闭 SSE 缓冲并配置足够的读取超时，例如在现有 Nginx 的
server 中加入下列 location（仅为配置参考，未应用）：

```nginx
location /agent-api/ {
    proxy_pass http://127.0.0.1:8001;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 180s;
}
```

## 完整流程与断线核对

已增加审批草稿暂存、写入回执和执行状态查询，详见 [完整验收说明](ACCEPTANCE.md)。
前端会在停止接收或流式失败后只读核对服务端状态，不自动重复提交写入。
域名 Nginx 路径示例见 `deploy/nginx-agent.location.conf`。
# 知识库管理入口

## 会话图片

`POST /agent-api/conversations/{id}/images` 接收原始 JPEG/PNG/WebP 二进制，要求现有登录 Cookie 和 X-Agent-Request。
前端先将最长边缩至 1600 像素，压缩成 JPEG。服务端验证上传不超过 2MB、最多 400 万像素、单帧图片。
返回 id/url/expires_in，发送消息时在 JSON 中附 image_id。图片仅属于本聊天，正文历史保存内部图片链接。
`GET /agent-api/conversations/{id}/images/{image_id}` 重新检查会话归属，失效返回 410。

图片写入 .local/images 磁盘目录，索引为 images.sqlite3。一小时后清理，删除或清空聊天时清理对应图片。
每会话最多 10 张、全局最多 100 张。上传、识别、生图共享单并发闸门，忙时立即提示重试。
生成图片允许最多 8MB，接收供应商 Base64 JSON 最多 12MB；服务器临时内存使用仍需实测。
Pillow 验证图片，识别通过独立 MiniMax 多模态请求完成，Base64 不写入会话和 checkpoint。
生成通过 image-01，主模型调用 generate_image 后用受鉴权链接在聊天中展示。每轮生图工具最多一次。
SSE 新增 image_status，action 为 analyze_image 或 generate_image，用于显示真实执行状态。
前端预览支持放大，过期后提示重新上传，图片不自动成为长期知识或公开博客配图。

Nginx 的 Agent location 需设置 client_max_body_size 3m。旧的 1MB 默认值可能拒绝合法上传。

## 联网搜索

默认启用 MiniMax 内置 web_search，`WEB_SEARCH_ENABLED=false` 可关闭，修改后重启服务。
Agent 通过普通 `web_search` 工具调用独立 MiniMax 搜索请求，仅发送查询词；
返回最多 5 条去重后的网页及摘要，不使用搜索子模型自行生成的答案。
正文由主模型继续流式输出，使用 Markdown 网页链接标注来源。
SSE 新增 `search_status` 事件，前端显示“正在联网搜索”；旧的 done/delta 协议保持兼容。

每轮最多调用搜索工具两次，每次最多两个 HTTP 请求处理 pause_turn；每个请求超时 60 秒。
这些是本地入口限额，MiniMax 在单次请求内部执行几次搜索由供应商控制。
搜索子请求的模型用量放在工具结果 usage 中，不包含在主模型调用用量统计内，不能将原统计视作总账单。
无结果、失败、未完成分别返回 no_results、unavailable、partial；回答需说明限制。
个人事实优先知识库，网页不得自动写入长期记忆，私密信息不得作为搜索词。
这项搜索词隐私要求由提示词约束，不代表已实现确定性敏感信息检测。

验证问题：“请联网搜索 Python 官方网站，给出来源链接”；个人查询：“我叫什么名字？”

博客聊天面板本人登录后，点击工具栏的「知识库」。沿用 Agent 的 Cookie 与本人密码，
访客不能访问以下管理接口。管理表单点击「确认保存」即为此次手动写入确认，
不再触发聊天工具的人工审批。聊天自动记忆仍使用原审批流程。

- `GET /agent-api/profiles?offset=0&limit=20`：本人资料列表，返回 items、has_more。
- `POST /agent-api/profiles`：新增资料，返回 201 与资料对象。
- `PATCH /agent-api/profiles/{id}`：保存编辑表单。
- `DELETE /agent-api/profiles/{id}`：永久删除；界面提供二次确认，可优先选择归档。

新增与编辑表单包含 content、category、visibility、status，拒绝额外字段；
归属由 require_owner() 校验，新增来源为 manual。编辑保留原来源。
公开且已发布的资料可供访客查询；改为私有、草稿、归档后立即从访客候选集移除。
编辑和删除沿用 KnowledgeStore 的旧向量失效机制，下次检索按需重建；
当前界面不提供索引进度、手动重建或文件上传。
