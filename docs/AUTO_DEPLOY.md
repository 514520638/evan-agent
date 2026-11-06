# 合并后自动发布

## 发布约定

普通开发只修改和验证代码。生产发布由对应仓库的 main/master push 事件触发。
合并 PR 会产生 push，因此会自动发布。直接推送到这两个分支同样会发布，可通过 GitHub 分支保护要求先走 PR。
PR 事件仅做测试或构建，不读取部署 Secrets。没有 workflow_dispatch 手动按钮。

三个仓库分别配置工作流，各自发布。Agent 主分支 main，博客与 Java 后端目前从 feature_agent 开发并合并到 master。
运行中的发布不取消，同仓库待发布任务由 Actions concurrency 调度。发布前比较远程分支 SHA，跳过已经过期的构建。
服务器 flock 锁让前端、Agent、Java 后端发布互斥，日志显示等待和获取锁的状态，最长等待 10 分钟。

## 一次性配置 Secrets

已在当前 Mac 生成部署专用 SSH 密钥并验证服务器接受。私钥路径为
`/Users/xdf/Desktop/project/evan-agent/.local/deploy-keys/github-actions`，已被 .gitignore 忽略。
服务器 authorized_keys 增加带 restrict 的公钥项，限制转发和终端申请。此密钥仍以 root 执行发布命令，应仅开放给可信发布工作流。
已从本机可信 known_hosts 导出服务器主机公钥，工作流强制验证主机身份。

在 514520638/evan-agent、514520638/evanlink-cn、514520638/evanlink-cn-serve 三个 GitHub 仓库分别打开
Settings → Secrets and variables → Actions → New repository secret。

| Secret | 内容 |
| --- | --- |
| DEPLOY_SSH_KEY | 专用私钥文件的完整内容，包含 BEGIN/END 行 |
| DEPLOY_KNOWN_HOSTS | .local/deploy-keys/known_hosts 的内容 |

在 Mac 用以下命令复制到剪贴板，不需要在聊天中提供内容：

```bash
pbcopy < /Users/xdf/Desktop/project/evan-agent/.local/deploy-keys/github-actions
# 粘贴到三个仓库的 DEPLOY_SSH_KEY，保存后再执行下一条
pbcopy < /Users/xdf/Desktop/project/evan-agent/.local/deploy-keys/known_hosts
# 粘贴到三个仓库的 DEPLOY_KNOWN_HOSTS
```

应用 API Key、博客密码继续只存放服务器 shared/agent.env，不需要添加到 GitHub。
Java 容器继续使用现有环境变量、网络和配置，不向 GitHub 添加数据库密码。
首次启用前，确认腾讯云安全组允许 GitHub 托管 runner 访问 SSH 端口 22；仅允许个人 IP 时 CI 会连接失败。
仓库 Actions 权限和分支保护须允许使用工作流。当前会话无法代你配置 GitHub Secrets，尚未运行真实 Actions 发布。

## Agent 流程

工作流 .github/workflows/deploy.yml：Python 3.12 → uv 0.9.25 → 锁定依赖 → unittest →
SSH 配置与连接预检 → amd64 Docker 镜像（标签为完整 commit SHA）→ gzip 压缩 → SSH 上传 → deploy/ci-deploy.sh。
镜像在 GitHub runner 构建，服务器只加载压缩镜像，不在 2GB 服务器编译或安装本地 Embedding 模型。
上传和部署是独立步骤，上传最多 32 分钟，部署最多 20 分钟。SSH 连接有 15 秒超时和保活检测。
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

## Java 后端流程

Java 17 → Maven clean verify → 上传 app.jar Artifact → SSH 预检、上传 → deploy/ci-deploy.sh。
服务器复用 evanlink-backend 容器，先确认它仍将 /opt/evanlink-backend/app.jar 挂载到 /app/app.jar。
备份旧 JAR，停止 Java 容器，覆盖同一 JAR 文件再启动，验证本机与域名的 /api/articles/filters。
覆盖时保留原文件 inode，避免单文件 bind mount 继续读取旧文件。失败自动恢复旧 JAR 并启动。
MySQL 容器、数据目录、Agent 和前端不随 Java 发布重启。Java 服务存在短暂停机。
当前 application.yml 的 ddl-auto=update 可能在启动时改变数据库结构；JAR 回滚不会逆转数据库变化。
涉及删字段或不兼容改表时，需要先单独备份数据库并设计迁移与回退方案。
后端仓库自身说明见 evanlink-cn-serve/deploy/AUTO_DEPLOY.md。

## 失败定位与重新执行

配置预检必须输出 SSH_READY 才会上传。错误示例：

| 日志 | 原因和处理 |
| --- | --- |
| Missing DEPLOY_SSH_KEY / DEPLOY_KNOWN_HOSTS | 在发生失败的那个仓库添加同名 Repository secret |
| No ED25519 host key is known / Host key verification failed | 主机公钥记录不匹配，重新复制已验证的 known_hosts 文件完整内容，不能填服务器 IP、私钥或用户公钥 |
| DEPLOY_SSH_KEY must contain an unencrypted OpenSSH private key | 粘贴 github-actions 文件完整内容，不是 github-actions.pub |
| Connection timed out | 检查安全组、22 端口或上传链路；不关闭主机身份验证 |
| Waiting for server release lock | 另一个仓库正在部署，等待完成；超出 10 分钟会失败 |

原 Upload and deploy 一直运行，可能只是上传未压缩镜像；它不代表服务已被停止。
新流程打印压缩包大小，scp 输出连接与传输诊断，之后独立显示 Deploy and verify。
确认旧任务仍在上传且要替换为新流程时，在 Actions 的运行详情中 Cancel workflow。
修正 Secret 后可以重跑相同提交；若修改了工作流或脚本，必须提交并合并新版本，重跑旧任务仍会使用旧提交。

## 启用和观察

先配置 Secrets，然后提交各仓库工作流和 deploy/ci-deploy.sh，通过 PR 合并到 main/master。
GitHub 仓库 Actions 页面查看测试、构建、上传、健康检查的日志。
不应一次提交与本次发布无关的工作区改动。本次未自动提交或推送分支。

服务器 Agent 备份位于 /opt/evan-agent/backups/ci-SHA-RUN，前端旧入口位于
/opt/evanlink-frontend/backups/ci-SHA-RUN。旧镜像、旧 assets 和备份持续保留，需要另行设置容量轮换。
Java 备份为 /opt/evanlink-backend/backups/ci-SHA-RUN/app.jar。
不要直接运行 docker system prune -a 清理仍用于回滚的镜像。

回滚 Agent：将 /opt/evan-agent/.env 的 AGENT_IMAGE 改为备份 image.env 中的旧标签，再 docker compose up -d。
回滚前端：将对应备份的 index.html 通过临时文件恢复为网站入口。旧 assets 保留期间可恢复旧版本页面。

上传使用 rsync --append-verify，日志显示字节进度。60 秒无传输活动会失败重试，每次最多 10 分钟，共三次；重试保留部分文件。总上传步骤限制 32 分钟。当前服务器已安装 rsync。传输速度仍取决于 GitHub runner 到服务器的网络，续传不保证提高带宽。
