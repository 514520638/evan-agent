# 合并后自动发布

## 发布约定

普通开发只修改和验证代码。生产发布由对应仓库的 main/master push 事件触发。
合并 PR 会产生 push，因此会自动发布。直接推送到这两个分支同样会发布，可通过 GitHub 分支保护要求先走 PR。
PR 事件仅做测试或构建，不读取部署 Secrets。没有 workflow_dispatch 手动按钮。

两个仓库分别配置工作流，各自发布。Agent 主分支 main，博客目前从 feature_agent 开发并合并到 master。
运行中的发布不取消，同仓库待发布任务由 Actions concurrency 调度。发布前比较远程分支 SHA，跳过已经过期的构建。
服务器 flock 锁让前端与 Agent 发布互斥。Java 服务保持现有发布流程。

## 一次性配置 Secrets

已在当前 Mac 生成部署专用 SSH 密钥并验证服务器接受。私钥路径为
`/Users/xdf/Desktop/project/evan-agent/.local/deploy-keys/github-actions`，已被 .gitignore 忽略。
服务器 authorized_keys 增加带 restrict 的公钥项，限制转发和终端申请。此密钥仍以 root 执行发布命令，应仅开放给可信发布工作流。
已从本机可信 known_hosts 导出服务器主机公钥，工作流强制验证主机身份。

在 514520638/evan-agent 和 514520638/evanlink-cn 两个 GitHub 仓库分别打开
Settings → Secrets and variables → Actions → New repository secret。

| Secret | 内容 |
| --- | --- |
| DEPLOY_SSH_KEY | 专用私钥文件的完整内容，包含 BEGIN/END 行 |
| DEPLOY_KNOWN_HOSTS | .local/deploy-keys/known_hosts 的内容 |

在 Mac 用以下命令复制到剪贴板，不需要在聊天中提供内容：

```bash
pbcopy < /Users/xdf/Desktop/project/evan-agent/.local/deploy-keys/github-actions
# 粘贴到两个仓库的 DEPLOY_SSH_KEY，保存后再执行下一条
pbcopy < /Users/xdf/Desktop/project/evan-agent/.local/deploy-keys/known_hosts
# 粘贴到两个仓库的 DEPLOY_KNOWN_HOSTS
```

应用 API Key、博客密码继续只存放服务器 shared/agent.env，不需要添加到 GitHub。
首次启用前，确认腾讯云安全组允许 GitHub 托管 runner 访问 SSH 端口 22；仅允许个人 IP 时 CI 会连接失败。
仓库 Actions 权限和分支保护须允许使用工作流。当前会话无法代你配置 GitHub Secrets，尚未运行真实 Actions 发布。

## Agent 流程

工作流 .github/workflows/deploy.yml：Python 3.12 → uv 0.9.25 → 锁定依赖 → unittest →
amd64 Docker 镜像（标签为完整 commit SHA）→ SSH 上传 → deploy/ci-deploy.sh。
部署脚本验证配置、备份 .env 和 Compose、停止 Agent、冷备份数据、切换版本并检查健康。
失败时恢复旧镜像和 Compose，保存故障数据以供排查，不自动覆盖当前数据为旧快照。
未来若有不兼容数据库迁移，必须为该迁移设计单独的恢复步骤。
服务存在短暂停机。资料和聊天不从开发电脑重新上传，生产数据目录持续使用。
健康检查只覆盖服务存活与代理可达，模型供应商可用性及审批等仍需要业务验收。

## 前端流程

Node 22 → npm ci → npm run build → GitHub Artifact → SSH 上传 → deploy/ci-deploy.sh。
脚本备份旧入口、检查 JS/CSS、先复制静态资源、最后原子替换 index.html，再验证域名与资源。
失败恢复旧入口，保留新旧哈希资源，单独上传的博客图片与 .well-known 继续保留。
上传产物不会包含 Git 忽略目录下的博客文章发布包。public 内容仍属于 Vite 静态构建输入。

## 启用和观察

先配置 Secrets，然后提交各仓库工作流和 deploy/ci-deploy.sh，通过 PR 合并到 main/master。
GitHub 仓库 Actions 页面查看测试、构建、上传、健康检查的日志。
不应一次提交与本次发布无关的工作区改动。本次未自动提交或推送分支。

服务器 Agent 备份位于 /opt/evan-agent/backups/ci-SHA-RUN，前端旧入口位于
/opt/evanlink-frontend/backups/ci-SHA-RUN。旧镜像、旧 assets 和备份持续保留，需要另行设置容量轮换。
不要直接运行 docker system prune -a 清理仍用于回滚的镜像。

回滚 Agent：将 /opt/evan-agent/.env 的 AGENT_IMAGE 改为备份 image.env 中的旧标签，再 docker compose up -d。
回滚前端：将对应备份的 index.html 通过临时文件恢复为网站入口。旧 assets 保留期间可恢复旧版本页面。
