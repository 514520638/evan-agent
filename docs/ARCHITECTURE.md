# 按任务读取代码

此文件描述当前仓库。历史规划在 [JARVIS-SPEC.md](../JARVIS-SPEC.md)，其中外部实验路径不属于本项目的可复现测试。

| 任务 | 首先读取 | 对应验证 |
| --- | --- | --- |
| 模型装配与入口 | [service.py](../src/evan_agent/service.py)、[main.py](../src/evan_agent/main.py) | test_auth_cli、test_persona |
| HTTP 与 Cookie | [api.py](../src/evan_agent/api.py)、[http_auth.py](../src/evan_agent/http_auth.py)、[API 契约](API.md) | test_api |
| 身份与权限 | [auth.py](../src/evan_agent/auth.py) | test_auth、test_auth_cli、受影响存储测试 |
| 聊天与审批状态 | [sessions.py](../src/evan_agent/sessions.py)、[memory_review.py](../src/evan_agent/memory_review.py) | test_sessions、test_memory_review、test_streaming |
| 知识存储与工具 | [knowledge.py](../src/evan_agent/knowledge.py)、[knowledge_tools.py](../src/evan_agent/knowledge_tools.py) | test_knowledge、test_knowledge_tools、test_persona |
| 审计、限额与摘要 | [middleware.py](../src/evan_agent/middleware.py) | test_middleware |
| 终端操作 | [terminal.py](../src/evan_agent/terminal.py)、[profile_commands.py](../src/evan_agent/profile_commands.py) | test_terminal、test_profile_commands |
| 开发工作流 | [开发规范](DEVELOPMENT.md)、[请求与上下文协议](WORKFLOW_PROTOCOL.md)、[任务协议](../scripts/workflow_protocol.py)、[知识与上下文](../scripts/workflow_knowledge.py)、[检查入口](../scripts/check.py) | test_workflow、test_workflow_protocol、doctor、全量 check |

机器可检索入口在 [上下文地图](context-map.json)；先查目录再按任务展开源码。
业务现状故事线在 [故事线目录](storylines/README.md)，复用经验在 [经验目录](experience/README.md)。两者均带来源版本，不将历史规划当作当前行为。

## 必须保持的边界

- 身份来自程序上下文，模型输入不能授予本人权限；实际写入时存储层再次授权。
- 登录会话与聊天 ID 分离；审批恢复、旧请求结果和历史提交都需要检查归属及有效性。
- CLI 与 HTTP 共用服务装配和会话状态机；不在路由层复制另一套记忆写入规则。
- 私人资料检索在 SQL 层过滤；测试替换模型和数据库，不使用本机个人数据。
- 流式显示、聊天历史提交、已完成的长期写入是三个不同事实，失败路径不能混同。
- 新契约必须同时检查其直接调用方和失败分支；不为通过测试更改未授权默认值。

涉及上述边界使用 full 档位；路线图目标不能替代当前契约。
