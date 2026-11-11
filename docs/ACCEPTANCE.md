# 博客记忆完整流程验收

## 步骤、目的与实现

| 步骤 | 目的 | 已实现 |
| --- | --- | --- |
| 补充 | 确保公司、职位等进入最终原文 | 前端合并补充字段；可暂存审批草稿 |
| 提交 | 只执行本人确认的内容 | 校验 Cookie、聊天归属、审批 ID、字段白名单 |
| 落库 | 确认数据真实保存，不依赖模型口头回复 | SQLite 提交后生成工具写入回执 |
| 访客查询 | 确认可公开资料能找到，私有资料不会泄露 | 实际搜索工具与 SQL 权限过滤 |
| 刷新 | 恢复身份、聊天与待审批补充 | Cookie + 聊天 ID + 服务端 draft_decisions |
| 断线 | 防止把已完成写入当作失败重试 | 只读核对 running/operation/writes，不自动重发 |

## 使用方法

1. 启动后端 `uv run evan-api`，博客运行 `npm start`。
2. 打开聊天窗口并本人登录，说“记住，我在北京工作”。
3. 补充公司、职位，选择公开范围与发布状态。
4. 若暂不提交，点击“暂存补充”；刷新并打开聊天，内容应恢复，确认框仍须重新勾选。
5. 确认处理后，查看数据库回执中的资料 ID、可见范围和发布状态。
6. 退出登录，用访客询问工作信息。只有 public + published 可被查询。
7. 可再分别录入 private + published、public + draft，确认访客查询不到。

暂存草稿没有写入个人资料库，也不会自动批准。未暂存编辑刷新前会提示离开确认。
“停止接收”后前端只刷新状态；服务端仍运行时短暂轮询，避免重复提交。

## 新增接口字段

- `PATCH /agent-api/conversations/{id}/approvals/{approval_id}/draft`：请求格式与审批 decisions 相同，仅暂存。
- 聊天详情增加 `running` 和 `operation`。
- operation 包含 id、status、writes；状态包括 running/completed/interrupted/awaiting_review/cancelled。
- writes 来自数据库工具返回的结构化 artifact，包括 id、saved/updated、visibility、publication_status。
- SSE done 增加 writes；流式连接每 15 秒发送注释心跳，前端不会将其显示为文本。

## 已完成的自动化验证

`uv run python -m unittest discover -s tests -v`

新增 `tests/test_blog_flow.py`，使用临时数据库及本地模型替身，覆盖：
补充后暂存与刷新、提交后重新打开数据库校验、访客真实工具查询、私有/草稿过滤、
HTTPS Cookie 属性和刷新、注销撤销、跨身份审批拒绝、写入后断线回执、旧审批重放拒绝、
待审批断流与取消。全套 68 项检查通过（2026-10-27）。
这些测试验证真实 Agent 工具节点、HTTP 路由与 SQLite，但不代表 MiniMax 自然语言理解和真实公网代理已验收。

## 域名代理卡点

2026-10-27 从当前执行环境直接访问 `https://evanlink.cn`：
`/agent-api/health` 返回 200 text/html，`/auth/me` 返回 200，`/auth/guest` 返回 405。
这条访问链路没有到达预期的 Agent API，可能落到了前端 SPA 回退。
浏览器若使用 Whistle 等本地代理，其链路可能与直接访问域名不同。

配置示例见 `deploy/nginx-agent.location.conf`。健康检查必须返回 JSON；无会话 me 返回 401 是正常的，随后 guest 应返回 200 并设置 Cookie。
后端 HTTPS 环境配置：

```dotenv
AGENT_ALLOWED_ORIGINS=https://evanlink.cn
AGENT_COOKIE_SECURE=true
```

本地 HTTP 联调可以继续使用原 localhost 来源和 Secure=false。修改环境变量后重启后端。
本次未登录腾讯云，也未自动修改线上或系统代理配置。

## 当前边界

- 会话、审批草稿、回执仍在内存；服务重启会失效，长期资料保留。
- 同一审批 ID 不能重复恢复；这不等于跨进程崩溃的数据库级 exactly-once。
- 已写入后流断开不会回滚资料；若没有拿到回执也不能断言一定没写入。
- 服务端关闭生成器可能需要等待正在进行的模型调用结束；断网不能瞬间取消已发出的外部请求。
- 自动化使用模拟模型；真实模型和域名代理还需要在实际部署链路上完成最终确认。
