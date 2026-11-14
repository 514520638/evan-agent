# Agent 模块导航

本模块负责个人数字分身、身份隔离、资料管理、检索与联网搜索。
输入为可信认证上下文及用户请求；输出为聊天文本、审批状态、资料回执及 HTTP/SSE。

| 文件 | 职责 |
| --- | --- |
| api.py | HTTP 边界、Cookie、SSE、管理接口 |
| main.py / terminal.py / profile_commands.py | CLI 交互与资料命令 |
| service.py | Agent 工厂与审批参数校验 |
| auth.py / http_auth.py | 本人密码与 Web 登录会话 |
| persona.py | 角色提示与工具过滤 |
| sessions.py / conversation_db.py | 会话隔离、持久化、审批恢复 |
| knowledge.py / knowledge_tools.py | 原文存储与模型工具 |
| retrieval.py | 分块、向量、混合检索 |
| web_search.py | 服务端搜索适配及来源 |
| middleware.py / lifecycle.py | 摘要、审计、限额与生命周期 |
| maintenance.py | 过期数据维护 |
| memory_review.py | 终端人工确认 |

依赖：MiniMax（回答与搜索）、硅基流动（向量计算）、SQLite（本地持久化）。
跨模块技术设计见 [AGENT_DESIGN.md](../../docs/AGENT_DESIGN.md)，数据结构见 [DATABASE_DESIGN.md](../../docs/DATABASE_DESIGN.md)。

## 博客统一身份与访客提问

- blog_identity.py：固定 Java 地址校验 HttpOnly 博客 Cookie，生成 AuthContext；不复制管理员密码，流中每 15 秒复验及检查撤销标记。
- guest_questions.py：独立 SQLite 保存访客问题与 UTC 时间，保留 183 天、最多 20,000 条，不随临时聊天过期删除。
- api.py：统一登录桥接、双端退出、管理员问题列表；实际接受处理的访客消息写入日志，主人消息不记录。
- maintenance.py：定时轮换访客问题。旧 HTTP 独立密码登录仅在显式兼容开关开启时可用，CLI 认证仍独立。

GuestQuestions 迁移地区字段，应用 lifespan 启停后台定位线程，api 记录问题时只写入待查询任务。只读列表不暴露原始 IP；后台完成后清除暂存 IP。
