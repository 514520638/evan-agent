# evanlink.cn 第一版 Agent 部署方案

## 现状与上线前提

服务器为 CentOS 7，2 核 2GB，x86_64，无 Swap，磁盘剩余 38GB；按用户决定不升级内存。
2026-11-03 最新观测：MySQL 重启后约 239MB，系统 available 约 851MB；此前 MySQL 约 746MB、Java 约 348MB。
采用 512MB 上限、单聊天并发的小流量试运行方案。重启后的低占用不代表长期峰值，需观察缓存预热后状态。
另有 game.evanlink.cn 服务，容器统计未覆盖全部宿主机进程。不要通过频繁重启 MySQL 维持内存余量。
不自动调整现有 Java/MySQL 配置。若其他服务增长挤占内存，先停止 Agent，而非继续开放流量。
CentOS 7 已停止维护，需要单独规划宿主机迁移；容器不替代宿主机安全更新。

已确认：Nginx 在宿主机，由 systemd 管理；博客静态目录 /opt/evanlink-frontend/html，
Java 经 127.0.0.1:8080/api/ 转发。Agent 新增 127.0.0.1:8001，不连接现有 MySQL。
首次迁移包括本地聊天和待审批记录。前端手动上传 dist，Java 发布流程保持不变。

## 目录与运行关系

```
/opt/evan-agent/
  compose.yaml
  .env                    # AGENT_IMAGE=evan-agent:发布标签
  shared/agent.env         # 真实 API Key 与生产配置
  shared/data/            # 容器 /app/.local
  backups/                # 受限访问的冷备份
```

镜像使用 Python 3.12、锁定 uv.lock 的依赖、非 root UID 10001。数据路径依赖 editable 安装，
不要自行改成非 editable wheel 安装，否则默认 .local 位置会改变。
初始内存上限 512MB、CPU 上限 1 核，属于待验证的试运行预算，不是已测量的最低需求。
生产设置 AGENT_MAX_CONCURRENT_REQUESTS=1，同一时刻仅执行一段 HTTP 聊天，其他请求返回 busy，用户稍后重试。
这不限制知识管理接口数量，也不预留宿主机内存。没有 Swap 时，其他服务仍可能引发宿主机 OOM。
单 worker，不能同时启动 CLI 和 API 使用同一会话数据库。

## 1. 构建并传输镜像

在本机或 CI 构建，不在内存紧张的生产服务器安装依赖。服务器已确认 x86_64，
必须构建 linux/amd64 镜像；本机 Apple Silicon 不要使用默认 arm64 镜像上传。

```bash
# 本机项目目录，先完成代码审核；镜像只包含 .dockerignore 白名单文件。
docker buildx build --platform linux/amd64 -t evan-agent:20261103-1 --load .
docker save -o /tmp/evan-agent-20261103-1.tar evan-agent:20261103-1
scp /tmp/evan-agent-20261103-1.tar root@服务器地址:/tmp/
scp deploy/compose.yaml root@服务器地址:/tmp/evan-agent-compose.yaml
scp deploy/agent.env.example root@服务器地址:/tmp/evan-agent.env.example
scp deploy/nginx-agent.location.conf root@服务器地址:/tmp/nginx-agent.location.conf
```

镜像跨架构构建依赖本机 Docker 的 buildx 支持。若网络无法访问镜像仓库或 PyPI，
应先解决构建网络，不在服务器上临时关闭 TLS 校验。

## 2. 完整迁移本地数据

先停止本地 evan-api/evan-agent，等待执行请求结束。审批可以保持待确认，但不能让写入执行到一半。
本地停止期间不要再开启聊天或编辑资料。确认以下文件存在：

- auth.sqlite3：本人密码哈希，迁移后沿用原主人密码，不再执行 evan-auth init。
- knowledge.sqlite3：个人资料原文。
- conversations.sqlite3：聊天、待审批元数据与 Cookie 哈希。
- conversations.checkpoints.sqlite3：审批恢复所需的图执行状态，必须与会话库成套迁移。

```bash
# 本机项目目录，服务全部停止后执行；归档中包含 SQLite 辅助文件及审计日志。
tar -czf /tmp/evan-agent-data-20261103.tar.gz -C .local .
chmod 600 /tmp/evan-agent-data-20261103.tar.gz
scp /tmp/evan-agent-data-20261103.tar.gz root@服务器地址:/tmp/
```

不要上传本机 .venv、Ollama 模型、整个 .env 或含明文密码的其他文件。
数据归档包含私密资料、聊天、认证哈希，须私密保存。只解包自己创建并核验的归档。
访客超过一小时的记录会在上线时清理；本人历史和待审批记录保留。
Cookie 仍按原到期时间失效，不延长登录。建议上线后重新登录本人账号再检查待审批。

## 3. 服务器初始化（首次部署）

以下步骤在维护窗口执行，先复查 available 内存与现有服务状态。已有 /opt/evan-agent 时先核对，不覆盖旧数据。

```bash
install -d -m 700 /opt/evan-agent /opt/evan-agent/shared /opt/evan-agent/backups
install -d -m 700 /opt/evan-agent/shared/data
install -m 600 /tmp/evan-agent-compose.yaml /opt/evan-agent/compose.yaml
install -m 600 /tmp/evan-agent.env.example /opt/evan-agent/shared/agent.env
tar -xzf /tmp/evan-agent-data-20261103.tar.gz -C /opt/evan-agent/shared/data
chown -R 10001:10001 /opt/evan-agent/shared/data
docker load -i /tmp/evan-agent-20261103-1.tar
```

手工编辑 shared/agent.env 填入真实 Key；不继承 Mac 的 HTTP_PROXY/HTTPS_PROXY。
创建 /opt/evan-agent/.env，内容为 `AGENT_IMAGE=evan-agent:20261103-1`，权限设为 600。
首次导入后暂时保留本地和迁移包备份；不要立即销毁唯一副本。

```bash
cd /opt/evan-agent
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs --tail 100 agent
curl -fsS http://127.0.0.1:8001/agent-api/health
docker stats --no-stream
free -h
```

容器名由 Compose 自动生成，规则是 `<项目名>-<服务名>-<序号>`：compose.yaml 顶层 `name: evan-agent`、
服务名 `agent`，因此容器名为 `evan-agent-agent-1`。`docker exec` 要用这个名字，
但更推荐 `docker compose exec agent ...`——用服务名，不受项目名或副本数影响。

修改 `shared/agent.env` 后必须 `docker compose up -d --force-recreate` 才会生效；
`docker compose restart` 不会重新读取 env_file。
运行中不使用 evan-agent CLI。需要检索诊断可运行
`docker compose exec agent evan-rag search '你的工作单位是什么'`，它不打开会话库锁。
若 OOM、持续重启或内存余量持续下降，停止 Agent 试运行并调整预算，不关闭 OOM 保护。
healthcheck 只检查 API 存活；Docker 不会仅因 unhealthy 自动重启，需要监控告警与人工排查。

## 4. 接入现有 Nginx

将 /tmp/nginx-agent.location.conf 中的 location 块加入
/etc/nginx/conf.d/evanlink.cn.conf 的 **443 ssl server**，放在 location / 前即可。
保留现有证书、/api/ 和前端 try_files，不修改 game.evanlink.cn 的配置。
先将原配置备份到 /opt/evan-agent/backups（不要复制为 conf.d 下另一个 .conf）。

```bash
nginx -t
systemctl reload nginx
curl -fsS https://evanlink.cn/agent-api/health
```

当前拓扑假定无 CDN，Nginx 覆盖 X-Forwarded-For 为 $remote_addr。容器仅通过回环发布端口，
并信任代理头，以便应用限流识别真实客户端；不要把端口改成公网发布后仍信任任意代理。
后续加 CDN 必须先配置可信 real_ip 来源，不能直接信任客户端自报头。
若 SELinux 导致 upstream 502，查看 AVC 日志后针对 Nginx 代理联网授权，不关闭 SELinux。
Compose 的 :Z 仅用于新建的 Agent 数据目录，不应用到其他项目目录。

### 手动发布博客 dist

先确保域名 /agent-api/health 正常，再发布前端。在本机 evanlink-cn 项目执行：

```bash
npm run build
tar -czf /tmp/evanlink-frontend-20261103.tar.gz -C dist .
scp /tmp/evanlink-frontend-20261103.tar.gz root@服务器地址:/tmp/
```

在服务器先备份现有前端，然后解包到一个本次新建的暂存目录。以下目录名如已存在，改用新发布号：

```bash
tar -czf /opt/evan-agent/backups/frontend-before-20261103.tar.gz -C /opt/evanlink-frontend/html .
mkdir /opt/evanlink-frontend/stage-20261103
tar -xzf /tmp/evanlink-frontend-20261103.tar.gz -C /opt/evanlink-frontend/stage-20261103
cp -a /opt/evanlink-frontend/stage-20261103/assets/. /opt/evanlink-frontend/html/assets/
```

核对 dist 中其他静态文件（favicon 等）并上传。保留原 assets，避免已打开的旧页面加载失败；
不要清空 html，因为它可能含证书续期目录。最后替换入口：

```bash
install -m 644 /opt/evanlink-frontend/stage-20261103/index.html /opt/evanlink-frontend/html/index.html.next
mv /opt/evanlink-frontend/html/index.html.next /opt/evanlink-frontend/html/index.html
```

检查 Nginx 用户可读，SELinux 开启时检查静态文件上下文。刷新网站验证新入口；
前端回滚时使用备份中的旧 index.html，保留新旧哈希 assets，避免影响仍打开的新页面。

## 5. 上线验收

1. 未登录管理接口返回 401；访客身份访问管理接口返回 403。
2. 访客问姓名、公司、教育经历：有资料引用，检索为 hybrid。
3. 本人登录后查看旧聊天和待审批；先核对内容再决定批准，禁止为测试随意批准旧事项。
4. 新增虚构测试资料，验证公开/私密/归档切换和访客可见性，测试后本人删除。
5. 新建审批测试：补充 → 提交 → 落库 → 访客查询；刷新待审批页面能恢复。
6. 联网搜索出现状态提示、流式文本、真实来源；断线后刷新能核对状态。
7. 确认前端、Java API、游戏站点没有回归，观察至少一个实际使用时段的内存和错误日志。

### 2GB 服务器的观察与退出条件

先连续执行访客问答、知识检索、联网搜索各数次，再在正常业务运行中观察至少 30 分钟，随后持续观察一天。
记录 free -h 的 available 和三个容器的 docker stats，不能只看 MySQL 刚重启时的数据。
将 available 持续低于 200MB 作为保守预警线（运维建议，并非精确容量保证）。
出现 OOMKilled、反复重启、Java/MySQL 错误增加或内存持续下降时：
`cd /opt/evan-agent && docker compose stop agent`，先恢复既有业务，再评估资源分配。
可用 `docker inspect --format '{{.State.OOMKilled}} {{.RestartCount}}' 容器名` 辅助检查；
历史 OOM 还需结合宿主机内核日志，不能只凭应用日志判定。

## 6. 备份、更新与回滚

第一版采用**短暂停机冷备份**，保证多个 SQLite 数据库的一致性。
每天低峰执行，也在每次更新前执行；需事先告知用户会有短暂维护。

```bash
cd /opt/evan-agent
docker compose stop agent
# 确认容器已停止、没有写入进程后执行；替换为本次唯一时间戳，不覆盖旧备份。
tar -czf backups/data-YYYYMMDD-HHMM.tar.gz -C shared/data .
chmod 600 backups/data-YYYYMMDD-HHMM.tar.gz
docker compose start agent
```

每日备份定时器待维护窗口确认后启用；建议保留 7 个日备份和 4 个周备份，并另存一份到服务器之外。
备份文件不受 183 天审计清理影响，当前代码没有自动备份轮换。
若 tar 失败也要恢复服务并排查，不把不完整文件当成功备份。

更新：预先构建、传输并 load 新标签 → 冷备份 → 修改 .env 的 AGENT_IMAGE →
`docker compose up -d --no-deps agent` → 健康检查与验收。始终保留上一版本镜像。
回滚：没有不兼容数据变更时换回旧镜像标签并 up；如涉及数据库迁移，先停止 Agent，
把现有 data 移到唯一的故障现场目录，再从对应版本的完整冷备份恢复 data 并设置 UID 10001。
不要直接覆盖正在运行的数据库，恢复旧备份会失去备份之后的新数据，操作前明确影响。

## 尚未执行与待确认

此方案不等于已上线：没有登录服务器，没有升级内存、改 Nginx、修改现有容器或迁移真实数据。
已确认：不升级 2GB、x86_64、宿主机 Nginx、前端手动上传 dist、迁移聊天与审批。待安排维护窗口。
MySQL 3306/33060 当前绑定所有网卡，是否公网可达还取决于安全组；单独核对，不擅自改业务连接。

参考：
- https://docs.astral.sh/uv/guides/integration/docker/
- https://docs.docker.com/engine/containers/resource_constraints/
