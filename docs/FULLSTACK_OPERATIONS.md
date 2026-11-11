# 全栈服务器部署与运维

![前端和 Agent 的发布顺序](figures/deployment.svg)

## 已确认拓扑（2026-11-03）

服务器 62.234.72.18，CentOS 7、x86_64、2 核 2GB、50GB 系统盘，无 Swap。
用户决定不升级内存。磁盘约剩余 38GB；MySQL 重启后系统 available 曾为约 851MB，但不是峰值容量证明。
Docker 26.1.3，Compose 2.27.1，Nginx 宿主机 systemd 管理。

| 服务 | 当前部署 | 入口/存储 |
| --- | --- | --- |
| 前端 | React/Vite dist 手动上传 | /opt/evanlink-frontend/html；Nginx 80 跳转 443 |
| Java | evanlink-backend，eclipse-temurin:17-jre | 宿主机 127.0.0.1:8080 → 容器 8080 |
| MySQL | evanlink-mysql，mysql:8.0；日志报告 8.0.46 | 3306/33060 绑定所有网卡；安全组是否允许公网需核查 |
| Agent | evan-agent-agent-1，evan-agent:20261103-1 | 127.0.0.1:8001 → 容器 8001；/opt/evan-agent/shared/data |
| 游戏站 | game.evanlink.cn，独立站点 | Nginx → 127.0.0.1:3100，不属于本次改动 |

## 统一入口与隔离

Nginx 的 evanlink.cn/www.evanlink.cn HTTPS server 使用现有 fullchain.cer 与 privkey.key。
`/api/` 保留 Java 转发；新增 `^~ /agent-api/` 保留完整路径转发至 8001；其余路径由 SPA try_files 接管。
证书私钥、API Key 和数据库密码不进入代码、前端 dist 或镜像。
agent-api 不是另一个仓库，也不需要另开容器。Java 后端无需转发聊天到 Python，浏览器直接走同域不同前缀。

Nginx search/chat SSE 要关闭 proxy_buffering 与 proxy_cache，read timeout 180 秒，应用每 15 秒保活。
当前无 CDN 前提下，Agent 路由覆盖 X-Forwarded-For 为 remote_addr，避免客户端伪造；以后接 CDN 要单独配置可信代理链。

## 前端构建和发布

在 Mac 的 evanlink-cn 运行 `npm run build`，产出 dist；这一步不自动上传服务器。
打包 dist → scp 到服务器 → 解包到独立 staging → 备份原站 → 复制 assets → 最后替换 index.html。
必须保留旧哈希资源，兼容仍打开的旧页面；不要清空网站目录里的证书续期目录。
项目本地开发代理仅供 Vite 使用，线上代理由 Nginx 实现。

本次实际故障：新 assets 已上传但 index.html 未替换，线上仍引用 index-115d4759.js，故无聊天入口。
改为新首页引用 index-7e8f6955.js 后用户确认生效。这些是本次发布哈希，不是以后必须固定的文件名。
排查时比较 staging/index.html、html/index.html、curl 域名返回的资源名，而不是一律让用户清缓存。

## Java 后端发布方案

已确认 Java 17、Spring Boot 3.2.0、Maven、Spring Data JPA、MySQL Connector。
代码分 controller/service/repository/model/config，外部 API 根路径 /api。
接口族包括 user_info、skills、articles、admin、album/photos、resume/verify。
源码更新通常通过 `mvn clean package` 生成 JAR，由现有 Java 17 容器运行。

当前未取得 Java Compose、JAR 挂载位置、MySQL 网络和数据卷配置，因此不能写出可信的最终覆盖路径。
实际发布前只读核对挂载，不输出容器环境变量中的秘密：

```bash
docker inspect evanlink-backend --format '{{json .Mounts}}'
docker inspect evanlink-mysql --format '{{json .Mounts}}'
docker inspect evanlink-backend --format '{{json .NetworkSettings.Networks}}'
```

建议流程：本机构建并验证 JAR → 备份数据库及原 JAR → 上传版本化目录 → 在现有 Compose 中切换版本 →
仅重建 Java 服务 → 检查原 /api 接口。不要为了更新 Java 重建或清空 MySQL。
若 JPA 配置自动更新 schema，应用回滚未必能恢复表结构，数据库变更要单独评估。

## Agent 发布方案

详见 [可执行部署步骤](DEPLOYMENT.md)。关键文件为根 Dockerfile、.dockerignore、deploy/compose.yaml、
deploy/agent.env.example、deploy/nginx-agent.location.conf。
Mac Apple Silicon 必须指定 --platform linux/amd64；本机 Docker Desktop 需要先启动。
基础镜像认证超时是镜像下载链路问题；曾单独拉取 Python 基础镜像后继续构建。

生产参数：512MB 内存上限、1 核 CPU、1 worker、AGENT_MAX_CONCURRENT_REQUESTS=1；
该并发只限制 HTTP 聊天执行，管理接口不受此容量信号量控制。
容器非 root UID 10001，代码只读，数据 bind mount 可写。SQLite 默认路径依赖 editable 安装，保持 /app/.local。
MiniMax 回答与搜索、硅基流动 Embedding 均走远端，服务器不运行 Ollama。

第一次迁移成套复制 auth、knowledge、conversations、checkpoints、向量与必要辅助文件，必须先停本机写入。
owner 密码沿用，待审批保持暂停；到期访客按策略清理。正式更新不可反复用本机旧库覆盖线上新资料。

## 已验证与待验证

| 项目 | 状态 |
| --- | --- |
| 镜像 amd64 构建 | 本机镜像检查通过 |
| 容器启动 | 用户报告 healthy |
| 127.0.0.1:8001 health | 用户报告 ok |
| HTTPS /agent-api/health | 用户报告 ok |
| 前端聊天入口 | 用户确认生效 |
| 线上真实模型、硅基流动查询 | 待线上业务验收记录 |
| 登录、审批恢复、私密资料隔离 | 本地测试覆盖；线上验收仍待记录 |
| 长时间资源占用、自动备份恢复 | 未提供线上验证结果 |

## 日常运维与回滚

```bash
cd /opt/evan-agent
docker compose ps
docker compose logs --tail 100 agent
curl -fsS http://127.0.0.1:8001/agent-api/health
free -h
docker stats --no-stream
```

启动最初 1 秒曾出现 connection reset，稍后 healthy 且 health=ok；应先查看启动日志，不要反复重启。
Nginx `invalid number of arguments` 的实际原因是上一行 Host 写成 %host 且缺分号，导致合并下一行解析。
Mac tar 的 provenance 扩展属性警告通常不影响文件；检查文件存在和入口引用后继续。

内存回升是运行观察重点，不能把 MySQL 重启当长期优化。available 持续低于 200MB 为建议预警线；
出现 OOM/重启/影响原业务，先 `docker compose stop agent`。原有博客和 Java 独立保留。
CentOS 7 后续应迁移受支持系统；MySQL 对外端口暴露需核对安全组与实际需求。

冷备份要先停止 Agent，成套备份 SQLite，完成后恢复服务；需保留异机副本。
备份定时任务和自动轮换尚未启用，审计 183 天清理不会帮忙清理备份。
回滚前端可恢复旧 index.html；回滚 Agent 用旧镜像标签，如涉及 schema 变更还需对应数据快照。
恢复旧快照会丢失此后新数据，先保留故障现场再恢复，不能在线覆盖 SQLite。
