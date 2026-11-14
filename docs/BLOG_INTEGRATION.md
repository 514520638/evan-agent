# 博客与 Agent 集成功能

## 登录

Java 为唯一浏览器身份权威：GET /api/admin/session 校验 Cookie，返回 role 和绝对过期时间。
Python 从 BLOG_API_BASE_URL 调用该接口，生产默认 https://evanlink.cn/api，本地需 http://127.0.0.1:8080/api。不得把 localhost 写入服务器容器配置。
Agent 不需要 APP_ADMIN_PASSWORD，也不把 Cookie 发给模型。博客授权不可用时不授予主人权限。
POST /agent-api/auth/logout 会调用 Java 撤销令牌，并清除两端 Cookie；Java /admin/logout 清除 Agent Cookie 并持久化令牌摘要，避免旧 Cookie 继续登录。
前端登录/退出通过事件与 storage 通知当前页面和其他标签页；只保存变更时间标识，不保存凭据。
HTTP 独立密码登录默认禁用；AGENT_ALLOW_LEGACY_WEB_LOGIN=true 仅供旧客户端过渡。CLI /login 保留。
公开聊天不要求登录，过期博客 Cookie 可以清除后回到访客。正在流式回复的主人身份每 15 秒向 Java 复验，应用内退出还会立即撤销内存上下文。

## 访客提问

文件 .local/guest_questions.sqlite3 随现有 shared/data 持久化挂载备份。保存问题原文和 UTC 时间，不保存 Cookie，不加入公开 RAG。新增地区功能仅临时保存待查询公网 IP，查询完成后清除。
仅记录已被服务接受处理的访客请求；超出并发/限流的请求、主人问题不记录。模型失败的访客问题仍属于已提出的问题，会保留。
记录保留 183 天并限制 20,000 条，后台每分钟清理。临时聊天一小时过期不会删除独立问题记录。
GET /agent-api/guest-questions?offset=0&limit=30 要求主人权限；首页管理员菜单和 Agent 会话管理面板提供查看入口。

## 发布与验收

依次发布 Java（新增会话校验、令牌撤销和点赞表）→ Agent → 前端。Java 现有 ddl-auto=update 会创建新增表；本地 validate 模式需等待目标数据库已有表，不应让本地调试代替生产迁移。
保持 APP_ADMIN_USERNAME/APP_ADMIN_PASSWORD 只在 Java 后端，BLOG_API_BASE_URL 在 Agent 环境配置。无需把密码复制给 Agent。
首次联调：访客提问→博客管理员登录→查看记录→打开 Agent 确认为主人→从 Agent 退出→确认博客也退出→用旧 Cookie 请求应拒绝。
本次未修改生产配置、未手动部署。

## 清理范围

移除未被工作流引用的 deploy/ci-pull.sh，保留固定本地运行环境的 ci-build-code.sh。
博客文章包 docs/blog-v*/ 被忽略，文件保留不删除。终端认证、审批、恢复和持久化兼容代码仍有用途，不按“看起来复杂”删除。

## 地区估算

新增 region、geo_status、client_ip 列，旧 SQLite 自动兼容迁移，历史问题地区为未知。前端显示时间、问题和估算地区，只有管理员可读。
后台单线程查询固定 HTTPS 服务 https://ipwho.is，仅发送 IP，字段限定国家、省、市。不查询/保存街道、门牌号或经纬度，不将问题内容发送给定位服务。
查询成功/失败后清除表内暂存 IP；未处理 IP 最长保留一天。内存缓存最多 1024 个 IP，成功 24 小时、失败 60 秒；每天最多 900 次外部查询，失败或超额显示未知。GUEST_GEO_ENABLED=false 可关闭定位。
来源使用 request.client.host：生产必须保持 Nginx 覆盖 X-Forwarded-For=$remote_addr，Agent 只暴露回环端口；不得直接向公网开放信任任意代理头的 Uvicorn。接 CDN 或新代理时需重新配置可信代理链。
地区查询不处在聊天等待链路，首次显示查询中，管理员面板自动刷新待定位条目。
隐私提示已更新；地区仅为 IP 归属估算，VPN、代理、移动网络会影响结果。定位服务可用性及配额以供应商为准： https://ipwhois.io/documentation 。
