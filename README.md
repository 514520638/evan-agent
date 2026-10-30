# Evan Agent

基于 Python、LangChain 和 MiniMax 的 AI Agent 起步项目，提供命令行多轮对话入口。

## 环境要求

- Python 3.11 或更高版本
- uv（推荐），或 Python 自带的 venv / pip
- MiniMax 开放平台 API Key，或有模型使用权限的 M Plan 订阅 Key

## 快速开始

```bash
uv sync
cp .env.example .env
```

首次使用时复制模板；已有 `.env` 时直接修改，避免覆盖现有密钥。填写：

```dotenv
MINIMAX_API_KEY=你的MiniMax密钥
MINIMAX_BASE_URL=https://api.minimax.cn/anthropic
MODEL=MiniMax-M3.1-Flash
NO_PROXY=api.minimax.cn,api.minimaxi.com
```

国内站使用上述地址；国际站密钥请将地址改为 `https://api.minimax.io/anthropic`。
模型填写原始 ID，无需 `openai:` 或 `anthropic:` 前缀。然后启动：

```bash
uv run evan-agent
```

请在项目根目录运行上述命令，程序从当前目录读取 `.env`。也可以运行
`uv run python -m evan_agent.main`。

聊天、资料录入和审批输入统一使用 `prompt_toolkit` 处理中文字符宽度、折行和
删除重绘。请在 VS Code 的集成终端中运行，而不是 Output 输出面板。
回车提交；Ctrl+A / Ctrl+E 移到行首／行尾，Ctrl+U 删除光标之前的内容。
普通输入不共享或持久化编辑历史，密码仍使用隐藏输入。升级代码后先运行
`uv sync`，再重启 Agent。非交互式管道输入使用标准逐行读取。

如果不使用 uv：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
# 编辑 .env，填写 API Key 后启动
evan-agent
```

输入 `exit` 或 `quit` 结束对话。模型通过 `.env` 中的 `MODEL` 配置，默认是
`MiniMax-M3.1-Flash`。使用 `langchain-anthropic` 访问 MiniMax 的 Anthropic
兼容接口，认证与地址显式读取 `MINIMAX_API_KEY`、`MINIMAX_BASE_URL`。
原来的 `OPENAI_API_KEY` 不参与调用。

M Plan 用户请从开放平台获取订阅 Key；普通聊天产品的会员是否包含 API
使用权限，需要以所购套餐为准。Key、站点与模型权限应匹配。
请求失败时会显示 HTTP 状态和服务端错误原因（隐藏当前密钥），方便区分
认证失败、额度不足和模型权限问题。

若配置正确但出现 `Connection error` 或 `SSL: UNEXPECTED_EOF_WHILE_READING`，
检查终端的 `HTTP_PROXY` / `HTTPS_PROXY`。本项目使用国内接口时可通过上述
`NO_PROXY` 让 MiniMax 域名直连，其余代理设置继续生效。如果已有 `NO_PROXY`
列表，请在原列表中追加域名；终端已导出的环境变量优先于 `.env`。

参考：[MiniMax 兼容接口](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)、
[订阅 Key 接入配置](https://platform.minimax.cn/docs/m-plan/other-tools)。

对话历史已使用 SQLite 持久化。Agent 使用 `create_agent`，
可以在 `src/evan_agent/main.py` 中添加工具与业务逻辑。

## 文件说明

- `pyproject.toml`：Python 项目元数据、依赖和命令行入口。
- `src/evan_agent/main.py`：Agent 创建与命令行对话。
- `src/evan_agent/auth.py`：本地主人密码、访客身份、登录会话及写入权限检查。
- `src/evan_agent/knowledge.py`：个人资料 SQLite 数据库、带权限检查的增删改查。
- `src/evan_agent/knowledge_tools.py`：关键词搜索、读取原文、本人对话写入和更新。
- `src/evan_agent/persona.py`：按可信登录状态选择数字分身／小磊角色及可见工具。
- `src/evan_agent/sessions.py`：按聊天 ID 隔离消息、校验归属及管理上下文。
- `src/evan_agent/lifecycle.py`：全部六类 Agent 中间件钩子及同步、异步实现。
- `.env.example`：环境变量模板，真实 `.env` 不提交到 Git。
- `uv.lock`：依赖版本锁文件，应提交到 Git。
- `pelican-bike.html`：独立的 SVG 鹈鹕骑行动画，可直接用浏览器打开。

## 本人身份认证（本地 CLI）

首次在本机交互式终端中设置主人密码：

```bash
uv sync
uv run evan-auth init
```

密码要求 12–256 个字符，输入时不回显；需输入两次确认。
初始化不需要模型 Key，也不会请求模型。已有主人账号无法重复初始化覆盖。

之后运行 `uv run evan-agent`，默认身份为访客：

| 命令 | 功能 |
| --- | --- |
| `/login` | 隐藏输入主人密码，通过校验后登录 30 分钟 |
| `/logout` | 退出本人身份，清空对话上下文 |
| `/whoami` | 查看当前身份与会话 ID |
| `/help` | 查看命令说明 |

不要把密码写在聊天内容或 `/login` 后面。认证命令由 Python 本地处理，
不发送给模型；聊天中自称“我是主人”不会改变身份。登录尝试、退出和会话
过期会切换身份；本人历史保留，重新登录可查看。CLI 启动仍先以访客身份开始。

凭据保存在项目 `.local/auth.sqlite3`，采用独立随机盐与 PBKDF2-SHA256
（600,000 次迭代），不保存明文密码。连续五次失败锁定 60 秒，计数跨进程
保存。`.local/` 与 `.env` 均已加入 Git 忽略规则。

可信身份通过 `create_agent(context_schema=AuthContext)` 和
`agent.invoke(..., context=identity)` 传递。未来知识写入工具必须在实际写入前
执行 `require_owner(runtime.context)`；该检查同时拒绝访客和已过期的本人会话。
个人资料存储层、`/profile` 管理命令和查询／对话写入工具已接入身份检查；向量检索尚未接入。

这一版适用于本机命令行，信任运行程序的操作系统账户；能修改代码或本地
认证数据库的人拥有本机控制权。已增加本地 HTTP Cookie 认证、聊天及审批接口，详见 [API 文档](docs/API.md)。
网页登录界面和密码找回功能尚未提供。

离线认证检查：`uv run python -m unittest discover -s tests -v`，不调用模型。

## 本地个人资料数据库

执行 `uv run evan-knowledge init` 建立 `.local/knowledge.sqlite3`。初始化可重复
执行，不会清空已有记录，也不需要 MiniMax Key。该文件由 `.local/` 忽略规则
排除在 Git 之外；这是本地文件权限保护，不是数据库加密或备份。

`profile_entries` 表字段如下：

| 字段 | 含义 |
| --- | --- |
| `id` | 随机资料 ID |
| `owner_id` | 来自 `require_owner()` 的稳定用户 ID，当前为 owner |
| `content` | 资料原文，保留原始空格和换行 |
| `category` | 自定义分类，默认 general |
| `source` | manual 手动／conversation 对话／import 导入 |
| `visibility` | private 仅本人／public 公开，默认 private |
| `status` | draft 草稿／published 已发布／archived 归档，默认 draft |
| `created_at` / `updated_at` | UTC 创建、更新时间 |

`KnowledgeStore` 提供 `create`、`get`、`list_entries`、`update`、`delete`。
新增、修改和删除在数据库事务中校验本人身份；归属不接受调用方传入或修改。
本人可查看自己的全部资料，访客只能读取公开且已发布的记录。私人记录与
不存在的 ID 对访客使用相同错误提示。列表支持分类过滤和分页。

更新保留创建时间并刷新更新时间；delete 是永久删除，想保留资料可改为
archived。对资料使用参数化 SQL 和字段约束；表结构由 `PRAGMA user_version`
标记为版本 1，未来升级需迁移，遇到未知版本拒绝打开以避免误写。
认证和个人资料分属两个数据库，通过认证层提供的稳定用户 ID 关联。

### 终端管理入口

运行 `uv run evan-agent`，输入 `/login` 登录后即可管理资料：

| 命令 | 功能 |
| --- | --- |
| `/profile add` | 分步输入原文、分类、公开范围、状态，确认后新增 |
| `/profile list [页码]` | 查看当前身份可见的资料，每页 10 条，默认第一页 |
| `/profile show 资料ID` | 查看原文和全部资料字段（不显示内部 owner_id） |
| `/profile edit 资料ID` | 编辑资料，空输入保留字段原值，确认后保存 |
| `/profile delete 资料ID` | 查看待删除资料，输入“删除”后永久删除 |
| `/profile help` | 查看操作说明 |

新增或编辑原文支持多行，单独输入 `/end` 完成；编辑时首行直接回车保留
原文。任一输入步骤可用 `/cancel` 或 Ctrl+C 取消并返回聊天。新增默认为
general 分类、private 可见范围、draft 状态、manual 来源。公开范围和状态
同时支持界面列出的中文及英文选项。保存前显示预览，输入“保存”才提交。
编辑会保留原始资料来源。

只有 public + published 的资料对访客可见；取消操作不会写入半条记录。
输入期间身份过期时，存储层仍会拒绝最终写入。资料退出登录和重启后保留，
它们与会话隔离模块中的临时聊天历史不同。

管理命令和交互输入不发送给 MiniMax，也不写入聊天上下文。这是手动管理入口，
普通闲聊不会自动保存成个人资料；本人明确要求记住或更新时可调用写入工具。
普通聊天已接入下面的查询工具，
尚未建立向量索引，未连接服务器。

### 两种交互身份与对话长期记忆

`PersonaMiddleware` 在每次模型调用前从 `runtime.context` 判断登录身份，
覆盖系统提示词，并控制模型能看到的工具列表。未登录时代表博客作者用
第一人称交流，明确为作者的 AI 数字分身；“你叫什么名字”“你的简历”
均按作者个人资料检索。登录后是私人助手“小磊”，用户本人信息仍从资料库查询。
聊天中自称本人或称呼“小磊”不能改变认证身份。

未登录时，“记住／保存／更新”等词按普通沟通处理，不启动记忆补全或审批，
也不主动提示登录或推荐管理命令。访客信息仅可用于当前聊天衔接，不记作作者
资料；只有访客明确询问是否永久保存时，才说明没有写入长期记忆。
写入工具对访客隐藏，存储层仍独立执行本人权限校验。

本人登录后可以直接说：

```text
小磊，记住：我最近在学习摄影，只供我自己使用。
小磊，记住并公开：我的英文名是 Evan。
小磊，把之前保存的居住城市更新为上海，保留原来的可见范围。
```

`save_personal_memory` 新增资料，默认 public + published、来源 conversation；
`update_personal_memory` 更新指定记录，默认保留可见范围及发布状态。
写入和更新都先经过人工审批；published 表示可用于问答，不等于 public。新增确认时直接回车采用公开、已发布；明确要求私有或草稿则保留该提案。
长期记忆就是 `.local/knowledge.sqlite3` 中的个人资料，`/profile` 是手动管理
同一批数据的入口，之后仍可查看、编辑或删除对话保存的记录。

访客的模型请求不展示写入工具；即使模型产生未展示的写入调用，存储层仍
通过 `require_owner()` 拒绝。每次写入都检查登录是否仍然有效。
提示词要求只处理本人明确的记住／更新意图，保存前查询重复和冲突，最后由人工审批选择公开范围和发布状态。意图理解和内容提取由模型完成，准确性需通过真实使用核对；
工具返回 saved/updated 后才能表示成功。不要把检索资料里的指令当成保存请求。
如写入成功后最终模型回复失败，数据库中的变更仍可能已经生效，可用 `/profile list`
核对后再重试。当前没有自动删除记忆或定时提醒工具。

### Agent 如何查询个人资料

已注册 `search_personal_knowledge` 和 `get_personal_knowledge` 两个只读工具。
询问主人的工作、经历、兴趣、项目等时，系统提示要求模型先检索再回答：

1. 模型将自然语言问题转换为简短关键词，例如“他有什么爱好”转换为“爱好 兴趣 喜欢”。
2. LangChain 从 `ToolRuntime.context` 注入登录身份，模型不能提供 role 或 owner_id。
3. SQLite 先按身份和发布状态限定候选资料，再用 `instr` 对原文及分类做字面匹配。
4. 多个空格分隔的关键词采用 OR 匹配，按命中词数、更新时间和 ID 排序，默认返回 5 条。
5. 工具返回原文片段、资料 ID、来源、更新时间及翻页信息；模型据此组织回答，并按提示标注 `[资料:完整ID]`。

访客只能检索 public + published；本人可检索自己的 private/public + published。
草稿和归档资料不用于普通问答，本人仍可通过 `/profile show` 管理查看。
系统提示用于引导工具选择和引用，模型是否遵循需在真实对话中观察；权限过滤
由数据库强制执行。模型没有命中时应说明“当前可查询的资料中没有找到”。

先通过 `/profile add` 录入资料，并将状态设置为 published；希望访客查询时
再把 visibility 设置为 public。随后可直接提问，例如“介绍一下主人的工作经历”。
控制台出现 `[知识查询] 本次返回 N 条可见的已发布资料。` 说明执行了搜索工具。

搜索片段每条最多 1600 字符，围绕首个命中词选取；详情工具每页最多 4000 字符，
可按 next_offset 继续读取。空关键词用于分页浏览可见资料。实际工具调用受
现有每轮模型／工具限额约束，不能保证一次遍历所有资料。

目前是关键词检索，不是向量语义检索，不自动分词或扩展同义词。两个中文字
可以匹配，`%` 和 `_` 按普通字符处理。模型可以换关键词重试，但仍可能漏检。
新增、修改、删除无需重建索引，下次查询立即读当前数据库；已经出现在旧聊天
历史中的信息不会因数据库更新自动删除，必要时使用 `/clear` 开启干净上下文。
查询命中的资料会作为工具结果发送给 MiniMax 用于回答，符合当前身份的私人
资料也不例外；`/profile` 本地管理操作自身仍不会发送资料给模型。

## 会话隔离

登录身份与聊天分开管理：`AuthContext.session_id` 表示本次登录或访客身份，
`conversation_id` 表示其中一段聊天。同一身份可以创建多段独立对话：

| 命令 | 功能 |
| --- | --- |
| `/new` | 创建并进入空白聊天，保留本次身份下的其他聊天 |
| `/sessions` | 列出当前身份的聊天，`*` 标记当前聊天 |
| `/switch 完整聊天ID` | 切换到当前身份有权访问的聊天并恢复其上下文 |
| `/clear` | 仅清空当前聊天，其他聊天不受影响 |
| `/whoami` | 查看身份、登录会话 ID 和当前聊天 ID |

例如在聊天 A 告诉 Agent 一个昵称，执行 `/new` 后的聊天 B 没有 A 的历史；
通过 `/sessions` 找到 A 的 ID，再执行 `/switch ID` 才恢复 A 的上下文。
所有读取、清空、切换和模型调用都会检查聊天归属；知道别人的聊天 ID
也不能访问。即使同为主人，两次独立登录之间也不共享短期对话。

持久化模式下，本人退出或登录过期保留历史，重新登录后可查看；访客退出清理其聊天，
并进入新身份的空白聊天。请求结束时再次检查身份有效期与会话是否仍存在，
避免退出或过期后显示尚未完成的旧请求结果。模型请求失败不提交本轮历史。
同一聊天的请求串行处理，不同聊天使用独立锁。

当前历史使用 SQLite 持久化，已接入 SqliteSaver
用于人工审批的暂停／恢复；每轮完成或取消后删除检查点；HTTP 到达待审批状态时保留检查点，供下一次请求恢复。完整消息（包括模型返回的工具调用信息）按会话保存；未来工具
可以从 `runtime.context.conversation_id` 获取当前聊天 ID。

## Agent 生命周期钩子

### 已接入的运行中间件

`src/evan_agent/middleware.py` 统一装配以下中间件，启动后自动启用。
它们已包含在现有 LangChain 依赖中，无需另行安装包。

| 中间件 | 默认行为 |
| --- | --- |
| `AuditedSummarizationMiddleware`（继承官方 `SummarizationMiddleware`） | 历史约 6,000 Token 时压缩早期消息，保留近期 10 条消息 |
| `AuditMiddleware` | 记录每轮关联 ID、聊天 ID、身份类型、耗时、调用数量、模型 Token 用量与异常类型 |
| `ModelCallLimitMiddleware` | 每轮最多执行 6 次模型节点调用，达到限制后结束该轮 |
| `ToolCallLimitMiddleware` | 每轮最多允许 8 次工具调用；超限时结束该轮，不执行该批尚未开始的工具 |

原有 `LifecycleMiddleware` 可用于调试；CLI 流式模式默认关闭终端阶段日志以避免打断回复，文件审计继续启用。已注册个人资料搜索和详情
工具，工具限额及工具审计会在模型调用它们时生效。可在 `.env` 覆盖默认值：

```dotenv
SUMMARY_TRIGGER_TOKENS=6000
SUMMARY_KEEP_MESSAGES=10
MODEL_RUN_LIMIT=6
TOOL_RUN_LIMIT=8
```

四个值均须为正整数。调用限额按每次 `invoke` 重置，不限制整个聊天的累计轮数。
限额不包含模型 SDK 内部重试或额外的摘要请求，因此不等同于账单请求次数上限。

摘要使用同一个 MiniMax 模型，会额外消耗额度；Token 阈值是近似估算，
不代表模型上下文硬限制。近期消息保留策略会兼顾工具调用／结果配对，
消息不足以切分时不会摘要。压缩后的消息由 `SessionStore` 存回当前聊天，
不影响其他聊天；原始早期消息会被摘要替代，这不是长期知识库或完整聊天归档。

审计文件为 `.local/audit/YYYY-MM-DD.jsonl`（UTC 日期），已由 `.gitignore`
忽略。每行一个 JSON 事件，包括 `agent.start/end`、`model.end/error`、
`tool.end/error` 和 `summary.end/error`。不记录正文、工具参数、结果、密钥或
原始异常消息。主模型用量来自返回的 `usage_metadata`；缺失时为 `null`。
摘要事件的用量为 `null`，因为官方摘要接口仅返回文本；不能把这些日志的
Token 合计当作完整账单。`agent.end` 只代表图正常结束（包括限额停止），
不保证业务成功；异常中断时可能没有该事件。日志按日分文件、不会随退出登录
自动删除；需要清理时可删除审计目录。日志写入失败会提示一次并继续聊天。

`LifecycleMiddleware` 在 `build_middleware` 的调试默认配置中注册；CLI 流式输出使用 `lifecycle_logs=False` 关闭它。
每次用户提问对应一次 `invoke`，生命周期从该次调用开始，到该次执行完成为止。

| 同步方法 | 异步方法 | 触发时机 |
| --- | --- | --- |
| `before_agent` | `abefore_agent` | 一次 Agent 执行开始前 |
| `before_model` | `abefore_model` | 每轮模型节点执行前 |
| `wrap_model_call` | `awrap_model_call` | 包裹每次模型调用 |
| `after_model` | `aafter_model` | 模型节点响应写入状态后 |
| `wrap_tool_call` | `awrap_tool_call` | 包裹单个工具调用 |
| `after_agent` | `aafter_agent` | 一次 Agent 执行正常到达结束阶段 |

当前终端使用同步 `invoke`，所以执行同步方法。切换为 `ainvoke` / `astream`
时使用异步方法；同一次调用不会把两套方法都执行一遍。
未调用工具时不触发工具钩子。工具执行后如果继续调用模型，会重复模型阶段。

节点钩子返回 `None` 保持状态不变；包裹式钩子原样返回下游结果。
日志记录阶段、消息数量、耗时及异常类型，不记录消息内容与密钥。
`.before`、`.after`、`.error`、`.end` 是包裹式钩子内部的日志标记，
不是额外的框架钩子。`after_agent` 不是异常清理钩子；调用清理使用 `finally`。

这是 `create_agent` 的中间件生命周期，不包含另一套用于追踪与流式 token
事件的 `BaseCallbackHandler` 回调。`dynamic_prompt` 是基于模型包裹钩子的
便捷装饰器，不是第七类生命周期阶段。
参考：[LangChain 自定义中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)。


## 长期记忆人工确认（HITL）

已配置 `HumanInTheLoopMiddleware` 拦截 `save_personal_memory` 和
`update_personal_memory`。模型提出工具调用后立即暂停，查询工具无需审批。
本人可以直接说：“小磊，记住，我在北京工作。”程序会：

1. 展示模型拟保存的原文，允许修改。
2. 若内容涉及工作，依次询问公司、职位、行业、薪酬、待遇；各项可直接回车跳过。
3. 选择 private/public；新增默认 public，回车采用，更新默认保留原值。
4. 选择 draft/published/archived；新增默认 published，回车采用，更新默认保留原值。
5. 展示完整预览，输入“确认写入”才执行；`/cancel`、Ctrl+C、输入结束或其他确认答案取消当前批次。

明确要求“仅本人”或“草稿”时，会在确认界面以对应提案为默认值。`/profile add` 手动入口与存储层的私有草稿默认值保持不变。

公司等补充作为正文保存，不臆造空白字段。公开范围作用于整条资料；若薪酬
需要私有、工作经历需要公开，应分为两条资料。界面在 public + published
时明确提示整条内容会对访客可查询。私人已发布资料只供本人查询；草稿只在
`/profile` 管理入口可见。审批中增加的内容会随工具结果用于模型生成最终回复。

`SessionStore` 为每一轮调用分配独立的临时 checkpoint thread ID；暂停和
`Command(resume={"decisions": [...]})` 恢复共用该 ID，身份与聊天归属仍由原有
会话仓库校验。审批决策使用 `edit`，将本人最终确认的参数直接交给工具执行，
不让模型再解释一遍。每次恢复与数据库写入都会检查身份是否过期。

一批多个写入必须全部审核后才恢复，取消其中任一项会放弃本批次。
同一轮此前已确认并完成的写入不会被后续取消回滚；多条写入不是跨记录事务。
终端没有审批回调时会拒绝继续；HTTP 返回待审批事件并保留暂停状态，等待后续审批请求。
程序重启会保留等待确认的审批；正在执行的请求标记为中断，不自动重放。网页审批接口已实现，前端表单待接入。


## 流式回答与当前 RAG 边界

终端使用 `agent.stream(stream_mode=["messages", "values"])`。只将 model 节点
AIMessageChunk 的文本部分即时输出，排除 thinking、工具参数和内部摘要。
输出不添加人为延时；实际片段可能包含多个字符，首次文本仍需等待模型思考
或工具查询。工具前的说明文字也可能流式出现。人工审批后通过 Command 恢复，
继续使用同一条流式处理路径。合成的限额提示没有 token 流时在结尾显示。

每个片段输出前检查会话和登录状态。网络中断时已显示的部分文字无法撤回，
但失败轮次不提交聊天历史；已获批准并写入的长期资料仍可能已经生效。
最终完整状态用于保存历史和审计；不使用屏幕上的片段拼接来替代完整消息。

目前一条 profile_entries 是一个检索单元：没有入库 chunk 切分、Embedding
模型或向量数据库。SQLite 对原文与分类做关键词子串匹配；搜索返回最多
1600 字符的片段，详情每页 4000 字符，这只是输出裁剪，不是索引切分。
MiniMax 是回答／摘要用的生成模型，不负责当前数据的向量化。

后续建议：短事实保留完整；简历按教育、工作、项目等章节切分；较长章节再
按段落和中文标点递归切分。可从每块 400–800 中文字符、重叠 80–120 字符
起步，用实际问答集调整。向量与关键词混合召回，过滤权限与发布状态，再
重排并返回带 profile ID 和 chunk ID 的证据。更新和删除资料时同步维护索引，
不要只新增向量；每块应记录资料版本及 Embedding 模型版本。


## 模型身份核对

基础模型身份由 `PersonaMiddleware` 从实际模型适配器的 model/model_name
读取并注入系统提示，不通过模型自己猜测。个人角色和底层模型 ID 分开描述。
查询底层模型不需要检索个人资料，也不应添加资料来源引用。
审计的 model.end/error 事件包含 requested_model 和 reported_models，后者来自
接口响应元数据，缺失时为空列表；它们用于核对请求与供应商返回的标识，
不是对供应商内部权重的验证。旧日志没有这些新增字段。


## 本地 HTTP 服务与博客接入

```bash
uv sync
uv run evan-api
```

访问 `http://127.0.0.1:8001/agent-api/docs` 查看接口文档。
共享服务装配位于 `service.py`，HTTP 入口为 `api.py`，Cookie 会话位于
`http_auth.py`。CLI 继续使用 `uv run evan-agent`，和 HTTP 共用 Agent 装配、
知识工具、中间件及会话执行状态机。

博客 `evanlink-cn` 已增加 `src/api/agent.ts` 及 `/agent-api` Vite 代理。
完整启动说明、接口清单、SSE 事件和审批请求示例见 [docs/API.md](docs/API.md)。
本版仅支持单进程；会话与等待确认的审批可在重启后恢复。未部署腾讯云。

## 聊天与审批持久化

默认 CLI 和 HTTP 服务启用 SQLite：

- `.local/conversations.sqlite3`：完整消息、归属、待审批与草稿、执行回执、HTTP 令牌哈希及到期时间。
- `.local/conversations.checkpoints.sqlite3`：LangGraph 检查点，恢复暂停的审批。
- `.local/conversations.sqlite3.lock`：进程占用锁，防止多个独立缓存覆盖同一数据库。

本人历史按用户 ID 归属，退出后保留，重新登录可查看；访客按随机 Cookie
会话隔离。HTTP Cookie 未过期时重启可继续使用；过期后重新登录，本人历史
仍然保留。密码和明文 Cookie 不存入聊天数据库。

审批草稿点击“暂存补充”后可跨刷新／重启保存。重启仅恢复待确认界面，不自动
执行写入。运行中断时可能已有工具落库，因此标记 interrupted 并保留已有回执，
不自动重放；请核对资料再提出新请求。显式清空聊天会删除消息和待审批检查点。

先停止旧服务，执行 `uv sync`，再启动 `uv run evan-api` 或 `uv run evan-agent`。
两个入口不能同时使用同一聊天数据库，多 worker／多实例暂不支持。
数据库文件自动创建，旧进程中仅存在于内存的历史不能自动迁移。
备份时停止服务后同时备份两个 SQLite 文件；文件权限为仅当前用户可读写，
但未做磁盘加密。本人历史暂不自动按期限清理。

## RAG 检索升级

当前已接入结构化字符切分、中文 BM25、Embedding 兼容接口、本地向量缓存、
RRF 融合和 MMR 算法重排。Embedding 未配置时明确回退 BM25，不冒充语义检索。
配置方法、前后对比与体验命令见 [docs/RAG.md](docs/RAG.md)。
