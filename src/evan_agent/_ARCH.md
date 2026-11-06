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
