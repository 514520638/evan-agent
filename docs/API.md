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
- 会话由服务端签发随机令牌，内存只存令牌哈希，不接受前端自行提交 owner_id/role。
- 本人有效期 30 分钟，访客 1 小时。登录成功轮换 Cookie；旧身份聊天及审批清理。
- 退出登录会撤销服务端身份，旧 Cookie 不能恢复访问。不存在公开注册主人接口。
- 博客已有管理员登录与 Agent 本人认证是两套独立认证，不自动共享权限。
- 登录失败返回 401；认证存储沿用现有五次失败锁定规则。

当前所有会话、历史、待审批 checkpoint 保存在内存；只能单 worker 运行。
重启后浏览器须重新建立会话，未确认写入不会自动执行；长期资料 SQLite 不受影响。

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
