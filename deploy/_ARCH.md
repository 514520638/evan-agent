# Agent 部署模块

负责独立 Agent 容器的运行配置与部署步骤，保持现有博客、Java、MySQL 服务独立。

| 文件 | 职责 |
| --- | --- |
| compose.yaml | 单 worker 容器、回环端口、持久化目录、资源与日志上限 |
| agent.env.example | 生产配置模板，不含真实凭据 |
| nginx-agent.location.conf | 宿主机 Nginx HTTPS server 的新增路由 |
| ci-deploy.sh | 接收 gzip 镜像，等待共享发布锁，备份并切换容器，失败恢复旧版本 |

依赖：evan_agent.api（聊天、认证、资料管理 HTTP 入口），.local（认证、会话、checkpoint、知识原文及缓存持久化）。
完整上线与回滚步骤见 [DEPLOYMENT.md](../docs/DEPLOYMENT.md)。
CI 的主机校验、超时、三仓库 Secrets 与启用步骤见 [AUTO_DEPLOY.md](../docs/AUTO_DEPLOY.md)。
CI 输入来自 .github/workflows/deploy.yml（Agent 验证、镜像构建与传输流程）。

ci-upload.sh：显示上传进度、检测传输停滞、最多三次断点续传；上传成功后才执行部署。依赖 runner 与服务器的 rsync。
