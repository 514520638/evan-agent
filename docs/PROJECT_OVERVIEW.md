# Evanlink 第一版：项目总览与阅读导航

基线日期：2026-11-03。依据三个本地仓库代码、用户提供的服务器配置和部署结果整理。
没有远程数据库连接或服务器登录，本文件不是线上全量审计报告。

## 发布结论

第一版已完成部署：用户确认 Agent 容器 healthy、宿主机和域名 health 返回 ok、博客新版聊天入口生效。
这证明发布与路由链路已接通。线上真实聊天、本人登录、审批恢复、私密隔离和持续负载仍需逐项验收，不能由 health 推导全部业务成功。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [架构与技术设计](AGENT_DESIGN.md) | 系统边界、Agent 分层、问答、记忆审批、RAG、搜索、故障恢复 |
| [数据库设计](DATABASE_DESIGN.md) | MySQL 实体、SQLite 字段、关系、数据生命周期、迁移 |
| [开发流程与知识点](DEVELOPMENT_GUIDE.md) | 从空壳到第一版、技术选择、日常开发、评估、排障 |
| [全栈部署总览](FULLSTACK_OPERATIONS.md) | 前端、Java、Python、Nginx 的职责与发布运维 |
| [Agent 部署操作手册](DEPLOYMENT.md) | amd64 镜像、完整数据迁移、配置、上线、备份与回滚命令 |
| [API 说明](API.md) | Cookie、聊天 SSE、审批、资料管理接口 |
| [RAG 专题](RAG.md) | 切块、向量、混合检索、评估、清理 |

## 三个仓库与四个常见名称

| 名称 | 仓库 | 职责 | 部署形态 |
| --- | --- | --- | --- |
| 博客前端 | evanlink-cn | 页面、文章、简历、相册、聊天与审批、本人知识库管理 | Vite dist，由宿主机 Nginx 提供 |
| 博客后端 | evanlink-cn-serve | 内容业务、管理员、简历验证、媒体与 MySQL | Java 17 容器，8080 |
| AI Agent | evan-agent | 推理、工具调用、身份、记忆、检索、搜索 | Python 容器，8001 |
| agent-api | 同一个 evan-agent 仓库 | Agent 的 FastAPI HTTP 入口 | /agent-api 路由前缀，不是第四个独立服务 |

三仓库无需为了部署同一机器而合并。它们以 HTTP 协作，Java 与 Python 各自管理数据与认证。
Java 管理员登录不等于 Agent 本人登录，博客 user_info 也不会自动同步到个人知识库。

![一个域名下的博客和 Agent](figures/architecture.svg)

## 功能范围

- 博客：主页、文章列表与详情、编辑器、关于、管理员资料、相册。
- Agent：访客数字分身；本人登录后助手叫小磊；多聊天隔离、流式输出、历史恢复。
- 长期记忆：对话提出写入，经人工补充及确认；本人管理表单可直接确认保存。
- RAG：权限过滤、切块、真实 Embedding、缓存、BM25 + 向量融合、MMR、多方向检索。
- 搜索：MiniMax 内置搜索通过普通工具封装，真实来源追加至回答。
- 运维：单进程锁、调用限制、审计、访客过期清理、模型切换缓存清理、日志保留 183 天。

## 当前边界

不是多租户平台；只有一个主人。没有多实例共享状态、专用 ANN 向量引擎、神经重排、文件导入、自动备份轮换、统一 Java/Agent 登录或严格的回答事实证明器。
首次公开试用应控制流量。2GB 共享服务器、CentOS 7 停止维护、MySQL 对所有网卡映射端口是仍需持续关注的运行条件。

## 源码证据与维护入口

| 主题 | 权威源码 |
| --- | --- |
| 博客页面与路由 | [router/index.tsx](../../evanlink-cn/src/router/index.tsx) |
| 博客聊天及知识管理 | [AgentChat](../../evanlink-cn/src/components/AgentChat/index.tsx)、[KnowledgePanel](../../evanlink-cn/src/components/AgentChat/KnowledgePanel.tsx) |
| HTTP/SSE 客户端 | [api/agent.ts](../../evanlink-cn/src/api/agent.ts) |
| Java 技术依赖 | [pom.xml](../../evanlink-cn-serve/pom.xml) |
| MySQL 实体 | [model](../../evanlink-cn-serve/src/main/java/com/evanlink/model) |
| Java 业务边界 | [controller](../../evanlink-cn-serve/src/main/java/com/evanlink/controller)、[service](../../evanlink-cn-serve/src/main/java/com/evanlink/service) |
| Agent 入口与组装 | [api.py](../src/evan_agent/api.py)、[service.py](../src/evan_agent/service.py) |
| 会话与恢复 | [sessions.py](../src/evan_agent/sessions.py)、[conversation_db.py](../src/evan_agent/conversation_db.py) |
| 检索与原文 | [retrieval.py](../src/evan_agent/retrieval.py)、[knowledge.py](../src/evan_agent/knowledge.py) |
| 搜索与维护 | [web_search.py](../src/evan_agent/web_search.py)、[maintenance.py](../src/evan_agent/maintenance.py) |
| 部署配置 | [Dockerfile](../Dockerfile)、[compose.yaml](../deploy/compose.yaml) |

跨仓库相对链接假设三个仓库位于同一父目录。文档按 fractal-knowledge 的职责、输入、输出分层维护，
模块导航见 [src/evan_agent/_ARCH.md](../src/evan_agent/_ARCH.md)，机器可读边界见 [knowledge.yaml](../knowledge.yaml)。
接口、状态、保留期或部署目录变化时，应同步更新对应专题，而不是复制一份新的互相冲突的说明。

## 已有质量证据

历史 19 题三轮真实评估共 57 次，教育题当时待确认而排除，规则评分为 53/54，通过率约 98.2%；Top3 检索命中为 39/39。
评分规则在人工检查误判后调整，另识别一次未有依据的“日常工作两种框架都用”扩写。
这不是独立盲测或全域准确率。教育资料后来已确认本科并更新；完整新基线需要重新运行。
硅基流动迁移后 8 个检索问题命中预期资料。联网搜索已做真实服务层验证。
部署后端 health 和页面入口已由用户确认，浏览器全部业务验收结果尚未提供。
