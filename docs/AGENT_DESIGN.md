# AI Agent 架构与技术设计

## 1. 目标与设计约束

目标：以作者资料回答访客问题；本人可通过聊天维护知识，且写入需人工确认。
约束：单主人、低成本、2 核 2GB 与其他服务共用、独立 Python 服务、HTTP 接入已有 React 博客。
主模型 MiniMax-M3.1-Flash；向量模型硅基流动 BAAI/bge-m3；服务端 Python + LangChain/LangGraph + FastAPI。

## 2. 模块分层

| 模块 | 输入/输出与职责 |
| --- | --- |
| api.py | HTTP/Cookie → 可信身份与会话操作；SSE 输出、来源校验、限流 |
| main.py / terminal.py | CLI 命令与中文输入 → 同一 AgentService/SessionStore |
| service.py | 配置、知识库、会话仓库 → 组装模型、工具、中间件 |
| auth.py / http_auth.py | 密码/随机 Cookie → AuthContext；禁止用聊天文本声明身份 |
| persona.py | AuthContext → 访客或本人提示词及工具可见范围 |
| sessions.py | 身份、conversation_id、输入 → 消息、执行状态、暂停审批与结果 |
| conversation_db.py | 会话状态 → SQLite；进程独占锁，启动恢复 |
| knowledge.py | require_owner 校验后的 CRUD；访客可见性 SQL 过滤 |
| knowledge_tools.py | ToolRuntime 身份 → 查询/详情/记忆新增/更新工具 |
| retrieval.py | 可见原文与问题 → chunk、BM25、向量、RRF、MMR、资料片段 |
| web_search.py | 查询词 → MiniMax 服务端搜索 → 真实网页与来源 |
| memory_review.py | CLI 人工审核表单；HTTP 审核由博客与 API 配合完成 |
| middleware.py / lifecycle.py | 摘要、审计、角色、HITL、模型及工具限额、生命周期观测 |
| maintenance.py | 每分钟巡检 → 过期访客清理、旧向量清理、审计日志过期删除 |

## 3. 一次问答

![访客问答与检索](figures/conversation.svg)

本轮模型可能先发文本再调用工具；message_start 区分模型消息。最终 done 是最终文本依据。
HTTP 使用 POST + fetch 读取 SSE，支持 UTF-8 跨块；15 秒 SSE 注释保活。
UI 收到断流后读取服务器操作状态，不自动重放可能包含写入的请求。

## 4. 身份与会话

Agent 主人密码使用随机盐 PBKDF2-SHA256（600000 次），不保存明文；连续五次失败锁定一分钟。
主人登录有效期 30 分钟；访客会话固定 1 小时。Cookie 为随机令牌，服务器只存 SHA256 哈希。
生产 Cookie HttpOnly、Secure、SameSite=Lax、Path=/agent-api。写请求要求自定义头及允许的 Origin。
角色由后端注入，访客看不到保存/更新工具，存储层仍通过 require_owner 二次约束。

登录会话决定“谁在访问”，聊天会话决定“哪段上下文”；conversation_id 不等于登录凭据。
本人会话按稳定 user_id=owner 归属，重新登录可恢复；访客按随机 session_id 隔离。
API 默认最多 20 段可见聊天；清空保留聊天 ID。本人退出不删除历史。
CLI 和 HTTP 使用同一默认会话库，因此生产不能同时启动两者，也不能开多个 worker。

## 5. 长期记忆与人工审批

![长期记忆人工审批](figures/approval.svg)

默认对话新增 public/published；原始存储 create 默认 private/draft。本人可以在审批时更改。
暂存审批草稿不会写入知识库。approval_id 绑定所属会话，失效/重复审批会拒绝。
聊天写入失败或断线时，已经提交的资料可能存在，使用工具 artifact 中的回执确认，不根据模型口述猜测。
知识库管理界面的“确认保存”是显式手动确认，走 CRUD API，不重复进入聊天 HITL。
访客“记住”不构成永久写入意图，也不能将访客内容当成作者事实。

## 6. RAG 设计

1. 先用数据库可见性规则取资料：访客 public + published；本人可检索本人已发布资料。
2. 短事实不拆，长文优先段落/换行/句末切分，600 字符、100 字符重叠，保留字符偏移。
3. 中文双字和英文词计算 BM25；向量使用单独配置的兼容 /embeddings 接口。
4. bge-m3 dense 向量 1024 维；按 chunk 缓存到 SQLite，精确余弦搜索，初始阈值 0.3。
5. BM25 与向量各最多取 40 候选，通过 RRF（k=60）融合，MMR 抑制冗余；每条原文保留最佳块。
6. 职业+技术、简历等综合查询按工作/技术/身份拆分召回，轮流合并，返回前重查版本与权限。
7. 模型据证据回答，引用 [资料:完整ID]；未知字段明确未知，不从毕业时间推断工作年限。

默认 Embedding 每批 8 条，超时 120 秒；服务失败回退 BM25，并在元数据说明原因。
multi_query 有分支失败时可返回 mixed；browse 不调用 Embedding。
向量缓存指纹绑定地址、模型名、切块版本；更换配置并重启后清除非当前指纹。
同名模型背后更换权重无法自动识别。缓存与原文不是跨库原子事务，权限过滤与版本检查用于减少陈旧内容返回。
现阶段不是大规模向量数据库，无 ANN、Cross-Encoder、bge-m3 sparse/ColBERT 路径。

## 7. 联网搜索

web_search 是普通 Agent 工具，其内部调用 MiniMax 原生 web_search_20250305。
只传查询词，独立 HTTP 请求避免 SDK 结果类型兼容警告；最多两次请求处理 pause_turn。
返回最多 5 条去重网页，每条摘要最多 2000 字符；拒绝非 HTTP(S) 或包含用户信息的 URL。
主模型据结果回答，程序附加本轮真实来源链接；search_status 显示搜索状态，来源随最终文本持久化。
不自行访问返回 URL，不自动写入长期记忆。网页指令与敏感搜索词防护主要依赖提示词，并非确定性 DLP。
每轮最多调用该工具 2 次；供应商每个请求内部的检索次数不受此本地次数精确约束。
搜索子请求用量不包含在主模型审计用量总计中，不能把原统计当完整账单。

## 8. 中间件、持久化与维护

模型每轮默认 6 次、全部工具 8 次；搜索另限 2 次。摘要触发约 6000 Token，保留近期 10 条消息。
生命周期 before_agent/before_model/wrap_model_call/after_model/after_agent 等用于诊断，审计保存元数据而非原始正文。
摘要控制推理上下文，不等于自动删除所有持久化数据。

操作状态包含 running、awaiting_review、completed、interrupted、cancelled。
启动将遗留 running 操作标记中断，不自动重放；等待审批的 checkpoint 保留。
正常完成、取消等路径清除 checkpoint。单进程文件锁防止两份内存状态互相覆盖数据库。
API 启动后台维护，每 60 秒扫描；访客到期立即失去访问资格，执行收尾后删除。
审计日志超过 183 天删除；本人历史和待审批无自动保留期限；数据库不自动 VACUUM。

## 9. 技术取舍与后续演进

SQLite 适合当前小数据、单实例部署，但限制多进程并发和共享状态；检索资料与历史增长会影响内存。
未来可分阶段增加后台索引任务、持久化查询分页、跨进程数据库、任务队列，而不是一次性引入全部组件。
优先改善无依据扩写、真实引用对应关系、评估规则和运行监控；只有评估证明需要时再加神经重排。
