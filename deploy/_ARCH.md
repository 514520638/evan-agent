# Agent 部署模块

负责独立 Agent 容器的运行配置与部署步骤，保持现有博客、Java、MySQL 服务独立。

| 文件 | 职责 |
| --- | --- |
| compose.yaml | 单 worker 容器、回环端口、持久化目录、资源与日志上限 |
| agent.env.example | 生产配置模板，不含真实凭据 |
| nginx-agent.location.conf | 宿主机 Nginx HTTPS server 的新增路由 |

依赖：evan_agent.api（聊天、认证、资料管理 HTTP 入口），.local（认证、会话、checkpoint、知识原文及缓存持久化）。
完整上线与回滚步骤见 [DEPLOYMENT.md](../docs/DEPLOYMENT.md)。
