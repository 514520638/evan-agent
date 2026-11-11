# 贾维斯式 AI Agent · 功能与模块拆分（LangChain 1.4 + LangGraph 路线）

> Python + LangChain · 纯文本（无语音输入输出）
> 目标：常驻、能主动、可观测、风险可控的桌面级 Agent

**本文取代 `FEATURE-SPEC.md`**（那份是按 langchain 0.3.30 写的，与实际项目栈不符）。

---

## 0. 实测基线（非推测）

以 `~/Desktop/project/evan-agent/.venv` 为准，全部现场验证过：

| 项 | 值 |
|---|---|
| Python | 3.14.6 |
| langchain | 1.4.6 |
| langchain-core | 1.6.12 |
| langchain-anthropic | 1.7.6 |
| langgraph | 1.2.16 |
| pydantic | 2.14.1 |
| SQLite | 3.53.2（系统 python3 是 3.51.0） |
| 模型接入 | `ChatAnthropic(base_url=https://api.minimax.cn/anthropic)` → MiniMax，工具调用正常，返回 thinking + signature |

**没装的**（决定了哪些能力要自己写）：

- `langchain-community` —— 社区工具一个都没有
- `langchain-openai` —— 你走的是 Anthropic 协议端点，用不上
- `langgraph-checkpoint-sqlite` —— 持久化 checkpointer/store 缺失（PyPI 有 3.1.2，**它同时提供 `langgraph.checkpoint.sqlite` 和 `langgraph.store.sqlite`**）
- `langchain-mcp-adapters` —— MCP 工具生态暂不可用
- 无 apscheduler / watchdog / chromadb / faiss / 任何向量库

---

## 1. 分层（L1–L5）

```
L5 接入层   终端 / HTTP / Webhook / IM 推送        → 统一成 Turn
L4 编排层   create_agent + middleware + 图状态     → 决定顺序，不写业务
L3 横切层   自定义 AgentMiddleware + 内置中间件     → 权限/审计/记忆/成本/降级
L2 能力层   @tool 工具集                          → 唯一对外动作出口
L1 适配层   模型 / 存储 / 调度器 / 事件总线 / 通道
```

**三条铁律**（沿用 dx-agent 家族已验证的）：

1. L4 决定「先做什么后做什么」，L3 只做横切；钩子之间不互相调用。
2. 对外只有一个 Response 出口，状态码/脱敏/审计从同一份事实推导。
3. 业务上下文走 `AgentMiddleware` 的 `request.state` / `request.runtime`，不靠全局变量。

---

## 2. 九大能力域

标注：**框架已给**（直接用内置中间件/工具）｜**要自建**（自己写）

| # | 能力域 | 核心职责 | 落点 | 框架已给 | 要自建 |
|---|---|---|---|---|---|
| D1 | 接入与通道 | 输入归一化成 Turn；结果推回去 | L5 | — | CLI/HTTP/Webhook/IM 适配器 |
| D2 | 理解与规划 | 意图路由 → 拆解 → 执行 → 验证 → 重规划 | L4 | `TodoListMiddleware`（自带 `write_todos`，等价于规划器）、`response_format` 结构化输出 | 意图路由、重规划策略 |
| D3 | 记忆 | 会话状态 / 长期事实 / 语义召回 | L1+L3 | `checkpointer`（会话）、`store`（长期 KV）、`SummarizationMiddleware`（上下文压缩） | 事实抽取器、SQLite FTS5 检索层 |
| D4 | 能力（工具） | 文件 / Shell / 网页 / API / 业务操作 | L2 | `@tool`、`ShellToolMiddleware`、`FilesystemFileSearchMiddleware` | 业务工具、工具注册表、风险标签 |
| D5 | 感知与触发 | 世界变化 → Event | L1 | — | 文件轮询、监控轮询、Webhook 入口 |
| D6 | 调度与运行时 | 定时/周期任务、后台队列、超时 | L1 | `ModelCallLimitMiddleware`、`ToolCallLimitMiddleware` | 调度器、任务持久化、后台执行 |
| D7 | 安全与权限 | 危险操作分级、拦截、审批、沙箱、审计 | L3 | `HumanInTheLoopMiddleware`（审批中断）、`ShellToolMiddleware`+`ExecutionPolicy`（沙箱）、`PIIMiddleware`（脱敏） | 风险分级表、路径沙箱校验、审计流水 |
| D8 | 观测与成本 | 每轮 trace、工具记录、token/成本 | L3 | `ModelCallLimitMiddleware`（限流）、langsmith（可选） | trace 落盘、成本统计、决策链记录 |
| D9 | 主动性与汇报 | 主动提醒、简报、异常通知 | L4+L5 | — | 触发策略、推送通道、定时简报 |

**去掉语音带来的减法**：D1 不需要 ASR/TTS/唤醒词/音频流；D2 不需要音频事件处理。
**换来的是**：所有输出都是文本 → 天然可被 D3 记忆、被 D8 审计、被 D9 主动推送，闭环更干净。

---

## 3. 各域落点详解

### D1 接入与通道（L5）

任何来源 → 一个 `Turn`；任何结果 → 推给用户。主动推送与用户输入走**同一条管线**，只有 `source` 不同。

### D2 理解与规划（L4）

- 意图路由：规则优先（关键词/正则）→ 模型兜底。命中「闲聊/知识问答」直接短路，不进工具循环。
- 任务拆解：不需要自己写 Plan-Execute 循环 —— `TodoListMiddleware` 直接给模型 `write_todos` 工具，让它自己列步骤并更新进度。
- 结构化输出：`create_agent(response_format=Plan)` 直接拿到 pydantic 对象，用于「把这个需求拆成步骤」「把这段话提成 JSON」这类任务。
- 澄清：信息不足先反问（贾维斯式：先确认再动手），不要猜着干。

### D3 记忆（L1+L3）

| 记忆 | 载体 | 说明 |
|---|---|---|
| 会话状态 | `checkpointer=InMemorySaver()` | 换持久化就装 `langgraph-checkpoint-sqlite` |
| 长期 KV | `store=InMemoryStore()` | 同一个 wheel 里还有 `langgraph.store.sqlite` |
| 长期事实 | 自建 SQLite 表 | 「用户叫 X」「项目用 Y 框架」 |
| 语义召回 | 自建 SQLite FTS5 | trigram 分词，**3 字以上走 MATCH** |
| 上下文压缩 | `SummarizationMiddleware` | 超长会话自动摘要，保留最近 N 条 |

⚠️ **实测边界**：FTS5 trigram 对 **2 个中文字符失效**（`MATCH` 返回空），必须留 `LIKE '%q%'` 兜底分支。3 字及以上 MATCH 正常。

### D4 能力（L2）

工具统一用 `@tool` 装饰器（产出 `StructuredTool`），每个工具声明三件事：名称/描述、输入 schema、**风险等级**。风险等级直接驱动 D7 策略。

`ToolRuntime`（从 `langchain.tools` 导入，**不是** `langchain_core.tools`）可在工具里拿到：`state` / `context` / `config` / `store` / `tool_call_id` / `stream_writer` / `tools` —— 工具能读会话状态、能写长期记忆、能流式回传。

### D5 感知与触发（L1）

只产出 `Event`，不直接执行。Event 进总线，由 D2 判断要不要升级成一轮对话。这样「监听到了」和「回应了」是分开的，不会因为文件频繁变动就疯狂调模型。

### D6 调度与运行时（L1）

- 限流：`ModelCallLimitMiddleware(thread_limit=8)`、`ToolCallLimitMiddleware(thread_limit=3)`（注意没有 `limit=` 这个参数名）。
- 定时/周期任务：自己写（threading + heapq + SQLite 持久化），无 apscheduler。
- 任务状态机 pending→running→done/failed，启动时把 running 复位为 pending。

### D7 安全与权限（L3）

**三层防线**，按顺序：

1. **硬拦截**（自己写 middleware）：在 `after_model` 里检查路径/命令黑名单，把违规的 tool_call 摘掉并回灌一条 DENIED 的 ToolMessage。
2. **人工审批**（框架给）：`HumanInTheLoopMiddleware(interrupt_on={'write_file': True})` → 图中断，等用户决定。
3. **执行沙箱**（框架给）：`ShellToolMiddleware(workspace_root=..., execution_policy=CodexSandboxExecutionPolicy())`。

⚠️ **四个实测坑**：

- **【最关键】`after_model` 钩子按列表【逆序】执行**。源码 `langchain/agents/factory.py`：
  `graph.add_edge("model", middleware_w_after_model[-1].name + ".after_model")`，再从后往前串。
  → **硬拦截中间件必须放在 middleware 列表的【最后】**，它的 `after_model` 才会先于 HITL 跑。
  实测对照：Guard 放最后 → `/etc/hosts` 被**直接拒绝**（无审批弹窗）；Guard 放前面 → 弹**审批框**（危险动作被降级成「问一句」）。
- **护栏违规记录要按轮重置**。跨轮累计会让后续无关的轮次状态码一直报 `denied`。
- 在 `wrap_tool_call` 里 **raise 会中断整轮运行**（不是变成工具错误），想「拒绝但继续」必须 `return ToolMessage(..., status='error')`。
- `request.tool_call` 是 **dict**（TypedDict），用 `request.tool_call['name']` 取值；`getattr(...,'name')` 拿到的是 None。
- **模型会绕过工具级黑名单**：实测拦下写 `/etc/hosts` 后，模型转头写了 `/tmp/add_x_to_hosts.sh` 和 `/tmp/hosts.with_x` 走别的路径。所以：① 拒绝信息要回灌给模型让它明白被拒；② shell 工具必须同样受控；③ 高危动作要靠审批而非纯黑名单。

### D8 观测与成本（L3）

`on_chain_*` 那一套在 1.x 已被 middleware 取代，采集点：
`before_agent` / `before_model` / `wrap_model_call` / `after_model` / `after_agent` / `wrap_tool_call` 六个钩子。

记录：traceId、每次工具调用、usage_metadata（token/成本）、分阶段耗时、决策链。输出到 JSONL 文件，**不要写进对话历史**（污染上下文还费 token）。

### D9 主动性与汇报（L4+L5）

定时简报、异常播报、长任务完成通知、待办跟进。走**完全相同**的 Pipeline（同一套 middleware、同一套降级、同一份审计），只是 `Turn.source='proactive'`。

---

## 4. 统一数据契约

```python
@dataclass
class Turn:
    text: str
    source: str          # 'user' | 'proactive' | 'webhook' | 'schedule'
    channel: str         # 'cli' | 'http' | 'im' | ...
    session_id: str      # 同时是 langgraph thread_id
    user_id: str | None = None
    trigger: dict | None = None
    attachments: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

@dataclass
class Response:
    text: str
    intent: str          # 'chat' | 'task' | 'proactive' | 'clarify' | 'confirm' | 'error'
    status: str          # 'ok' | 'degraded' | 'needs_confirmation' | 'denied' | 'error'
    steps: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    memory_written: list[str] = field(default_factory=list)
    trace_id: str = ''
    latency_ms: int = 0
    cost: float = 0.0
    error: str | None = None

@dataclass
class Event:
    kind: str            # 'file_changed' | 'webhook' | 'health' | 'timer'
    payload: dict
    severity: str = 'info'   # info / warn / critical
    dedup_key: str | None = None
    created_at: float = field(default_factory=time.time)
```

---

## 5. 目录结构（建议落在 evan-agent 项目下）

```
src/evan_agent/
  core/
    contract.py       Turn / Event / Response
    context.py        AppContext —— 贯穿全链路的业务上下文
    response.py       build_response —— 唯一出口
    degradation.py    降级链（单一事实源）
  agent/
    build.py          create_agent(...) 的唯一装配点（模型/中间件/工具/checkpointer 全在这）
    prompts.py        system prompt 模板
  middleware/         自定义 AgentMiddleware
    guard.py          风险分级 + 硬拦截（wrap_tool_call）
    audit.py          审计 + 成本统计
    memory_write.py   事实抽取 → 长期记忆
    observe.py        trace / 耗时
  tools/
    base.py           工具注册表 + 风险标签
    filesystem.py     read_file / write_file / list_dir
    shell.py          受控 shell（沙箱 + 黑名单）
    web.py            fetch_url / web_search
    memory_tools.py   remember / recall
  memory/
    store.py          SQLite 长期事实 + FTS5 检索（3+字 MATCH，2字 LIKE）
    extractor.py      从对话抽事实
  planner/
    router.py         意图路由（规则优先）
  scheduler/
    scheduler.py      一次性 + 周期任务（threading + heapq + SQLite）
  perception/
    watcher.py        文件轮询 + 去抖
    monitor.py        系统/接口健康
  bus/
    bus.py            事件总线 + TriggerPolicy（去抖/冷却/严重度门槛）
  channels/
    base.py           Channel 协议
    console.py / http.py
  entry.py            启动装配
```

---

## 6. 三条关键流程

### 6.1 普通问答（最短路径）

```
Turn → 路由(chat) → agent.invoke
     → before_model 注入记忆/画像
     → after_model 抽事实候选
     → after_agent → Response
```

### 6.2 多步任务（贾维斯式）

```
Turn → 路由(task) → agent.invoke
     → TodoListMiddleware 让模型自己列步骤
     → 每步: wrap_tool_call 授权(硬拦截/审批/沙箱) → 执行 → 结果回灌
     → after_agent → Response(steps=[每步记录])
```

### 6.3 人工审批（危险动作）

```
agent.invoke(...) → 命中 interrupt_on → 返回 '__interrupt__'
     → 把 action_requests 渲染成「提议」给用户
     → 用户批准 → agent.invoke(Command(resume={'decisions':[{'type':'approve'}]}), cfg)
```

⚠️ 恢复**必须**用 `Command(resume=...)`（dict + `decisions` 键）。用普通用户消息「确认」恢复会 400：`tool call result does not follow tool call (2013)`。
`resume` 传 list 会 `TypeError: list indices must be integers`（代码按 `['decisions']` 取值）。

### 6.4 主动推送

```
Timer/文件变动 → Event → TriggerPolicy(去抖+冷却+严重度)
     → 升级为 Turn(source='proactive') → 走完全相同的 Pipeline → Response → 推送
```

---

## 7. 参考实现（本仓库已跑通）

`jarvis/jarvis14/` —— 按本规格写的 M0+M1 骨架，866 行：

```
jarvis14/
  contract.py     Turn / Event / Response
  guard.py        硬拦截中间件（after_model 预拦截 + wrap_tool_call 兜底）
  tools.py        @tool 工具集（文件工具带路径沙箱、记忆工具）
  memory.py       SQLite + FTS5 长期记忆（3+字 MATCH，2字 LIKE）
  app.py          JarvisApp 装配（模型/中间件/工具/checkpointer）
  test_e2e.py     离线自测（桩模型，21 项断言全过）
  test_live.py    真实链路验证（MiniMax + HITL + 护栏 + 记忆，6 项全过）
```

跑法：

```bash
cd ~/Desktop/project/evan-agent
./.venv/bin/python ~/.openclaw/workspace/jarvis/jarvis14/test_e2e.py   # 离线
NO_PROXY='api.minimax.cn,api.minimaxi.com,localhost,127.0.0.1' \
  ./.venv/bin/python ~/.openclaw/workspace/jarvis/jarvis14/test_live.py # 真实链路
```

已验证通过的链路：普通问答 → HITL 审批中断（不落盘）→ 批准后落盘 → 拒绝后不执行 →
危险路径硬拦（`/etc/hosts` 直接拒绝，无审批框）→ 长期记忆写入与召回。

---

## 8. 路线图

| 阶段 | 目标 | 交付 | 验收 |
|---|---|---|---|
| **M0 骨架** | 管线跑通 | `core/` + `agent/build.py` + 2 个工具 + 1 个自定义 middleware | 一次对话走完，六个钩子全触发 |
| **M1 对话+工具** | 能干活 | 文件/Shell/网页工具 + `wrap_tool_call` 硬拦截 + `HumanInTheLoopMiddleware` 审批 + 审计 | 危险操作会拦、会问、会记，且模型绕不过去 |
| **M2 记忆+规划** | 越用越懂你 | 持久化 checkpointer/store + FTS5 长期记忆 + 事实抽取 | 追问能复用上下文；复杂任务能多步完成 |
| **M3 调度+主动** | 不用你开口 | 调度器 + TriggerPolicy + 推送通道 | 定时简报能推出来 |
| **M4 感知+多通道** | 全面感知 | 文件轮询 + Webhook + IM 适配 | 文件变动能触发处理 |

建议先做 **M0+M1**。Jarvis 类 Agent 90% 的失败不是模型不行，是「工具没边界」「状态对不上」「该问不问」。

---

## 9. 已踩坑清单（全部实测）

1. **`load_dotenv` 不覆盖已存在的环境变量** —— 你 shell 里 `NO_PROXY` 已经含 `api.minimaxi.com` 但**不含** `api.minimax.cn`，`.env` 里的 NO_PROXY 被忽略 → 请求走 Clash 代理 → `SSL_ERROR_SYSCALL` → `AnthropicConnectionError`。修：`load_dotenv(..., override=True)` 或显式把 `api.minimax.cn` 并入 NO_PROXY。
2. **`wrap_tool_call` 里 raise 会炸掉整轮**，要拒绝就 `return ToolMessage(status='error')`。
3. **`request.tool_call` 是 dict**，`getattr(tc,'name')` 恒为 None。
4. **模型会绕过工具黑名单**（实测改写路径/生成脚本）→ 高危动作必须靠审批 + shell 同控。
5. **HITL 恢复必须 `Command(resume={'decisions':[{'type':'approve'}]})`**，不能用普通用户消息。
6. **`dynamic_prompt` 只接受可调用对象**，传字符串报 `'str' object is not callable`；`lambda request: '...'` 可用，`lambda request, state: '...'` 参数个数不对。
7. **`ModelCallLimitMiddleware` 没有 `limit=` 参数**，只有 `thread_limit` / `run_limit` / `exit_behavior`。
8. **FTS5 trigram 2 个中文字符 MATCH 失效** → 必须 LIKE 兜底。
9. **`ToolRuntime` 从 `langchain.tools` 导入**，`langchain_core.tools` 里没有。
10. **没有 `langchain.prebuilt`**（1.x 拆走了），也没有 `langchain-community` —— 社区工具要自己写或装包。
11. **`after_model` 逆序执行**（见 D7）—— 硬拦截中间件必须放 middleware 列表最后，否则被 HITL 抢先。
12. **护栏违规记录跨轮累计**会污染状态码 —— 每轮开始要 reset。
