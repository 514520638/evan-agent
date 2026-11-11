# Agent 部署模块

当前 CI 使用固定本地环境镜像加代码包，运行数据独立持久化。

| 文件 | 职责 |
| --- | --- |
| compose.yaml | 单实例资源限制、端口和数据挂载 |
| ci-upload.sh | 代码包与部署配置续传 |
| ci-build-code.sh | 校验依赖、固定环境、本地生成代码镜像及导入检查 |
| Dockerfile.code | 清理旧源码后复制新源码，不安装依赖 |
| ci-deploy.sh | 共享锁、备份、更新、健康检查与回滚 |
| ci-pull.sh | 历史仓库拉取工具，当前 CI 不调用 |
| agent.env.example | 生产环境配置示例 |
| nginx-agent.location.conf | Agent 反向代理配置 |

输入来自 .github/workflows/deploy.yml；运行环境依赖根 Dockerfile 的 editable 安装布局。
完整当前流程及环境升级限制见 [AUTO_DEPLOY.md](../docs/AUTO_DEPLOY.md)。
