# 知识检索升级与体验方法

## 2026-11-05 延迟优化

综合查询按工作、技术、身份、原问题四个方向召回，同次请求共用可见资料快照和资料向量，
四个查询向量批量生成。取消先后两次原问题检索。权限与版本仍在返回前校验。
批量 Embedding 失败时，本轮各方向回退到 BM25，元数据标记 mixed。
这些缓存只存在于本次调用，不在不同身份或并发请求之间共享。

常规历史摘要只在消息尾部为用户输入时评估。工具回合继续使用本轮证据，
估算上下文达到 max(24000, SUMMARY_TRIGGER_TOKENS × 4) 时仍可应急摘要。
该阈值是应用压缩策略，不代表模型最大上下文保证。同步与异步入口共用此判定。
资料原文与完整记忆的写入规则保持原有行为，摘要依旧由原 LangChain 实现生成。

会话仅在写入回执增加时额外持久化，开始、审批、完成和中断的状态仍落盘。
已完成操作的 operation.timings 保存 first_text_ms 与 total_ms。首字时间按服务端开始执行到
第一个可见 delta 计算，非流式为 null，不包含浏览器网络传输和绘制耗时。

## 自动维护（API 服务）

`evan-api` 启动后立即启动后台巡检，每 60 秒执行一次，关闭服务时停止任务。
访客 Cookie 从创建起固定 1 小时过期（不因聊天自动续期），过期即不可访问；
后台删除令牌和访客聊天。执行中的聊天先撤销身份，等待执行收尾后再由下次巡检删除，
不会因清理器提前移除状态导致执行收尾失败。停机期间不运行，重启后补清。
本人登录过期只删除令牌，不删除本人历史、个人资料或待审批内容。

审计目录仅处理 `YYYY-MM-DD.jsonl` 普通文件，按 UTC 文件日期删除超过 183 天（半年）的日志；
跳过符号链接、不规范文件名和非法日期。不包含 Docker/Nginx 日志或备份文件。

Embedding 指纹由端点、模型名和切块版本构成。创建 Retriever 时自动删除非当前指纹的向量；
API 巡检也会执行此检查。修改 .env 后须重启服务，旧缓存被删后，首次查询生成新向量。
未配置 Embedding 时保留缓存；同端点、同模型名背后偷偷换权重无法自动识别。
向量清理不删除 knowledge.sqlite3 原文。SQLite 文件可能保留可复用空页，本次没有自动 VACUUM。
CLI 可触发模型切换清理，但不启动 HTTP 会话和日志定时任务。

## 当前状态

代码已接入 chunk、Embedding 客户端、SQLite 向量缓存、BM25、RRF 和 MMR。
本地方案使用 Ollama 的 bge-m3，通过 OpenAI 兼容接口接入。配置存在不代表服务可用，须通过实际查询确认 hybrid。
MiniMax-M3.1-Flash + /anthropic 是现有生成模型配置，不是本实现需要的 /embeddings 接口。

## 前后区别

| 环节 | 以前 | 现在 |
| --- | --- | --- |
| 检索单元 | 整条资料 | 短资料整条，长资料按段落／句子切块 |
| 文本匹配 | instr 子串命中数 | 中文双字与英文词的 BM25 |
| 语义召回 | 无 | 独立 Embedding 兼容接口，未配置则禁用 |
| 向量索引 | 无 | knowledge.vectors.sqlite3，精确余弦搜索，适合小型资料库 |
| 混合召回 | 无 | BM25 + 向量两个分支各取最多 40 个，通过 RRF 融合 |
| 重排 | 命中词数排序 | MMR 相关性／多样性重排；每条资料只取最佳块 |
| 证据 | 裁剪前后文 | 原文块、真实字符偏移、chunk_id、原始资料 ID |

默认块大小 600 字符、重叠 100 字符，优先双换行、换行、中文和英文句末标点。
这里的单位是字符，不是 Token。MMR 是算法重排，不是 CrossEncoder 神经重排模型。
本次没有安装或调用额外的神经重排服务。

## 如何启用与判断

```bash
uv sync
uv run evan-rag status
uv run evan-rag search '你主要从事什么工作'
# 本人查询私人已发布资料：
uv run evan-rag search '你主要从事什么工作' --owner
```

默认以访客权限查询，无模型生成过程，方便直接观察召回结果。
`mode=bm25` 表示未使用语义检索；`mode=hybrid` 表示本次 Embedding 分支正常运行。
`fallback_reason=embedding_not_configured` 表示尚未配置；embedding_unavailable 表示本次服务失败。
无关键词的空查询是 browse，不执行 Embedding。普通聊天也返回相同检索元数据。

Embedding 单独在 .env 配置，参数见 .env.example。必须是实际支持 embeddings 的
模型，服务响应须提供 data[index,embedding]。不能把 MiniMax 聊天型号填入这里。
如果使用本地兼容服务也可填写它的地址，若该服务不要求 Key 可以留空。
配置后重启 Agent。首次查询会向该服务发送当前身份可见的已发布资料块；
后续只对新增／变化的块计算向量，但每次查询仍需计算查询向量。
请先用虚构资料验证所选服务，不要在未确认供应商和服务权限前填写真实凭据。

## 可以体验的差别

1. 长资料：录入包含多个项目的已发布长文，查询尾部项目名。现在返回与命中项目
   对应的 chunk，而不是把整份长简历直接塞进上下文。
2. 检索排序：录入长短不同的工作资料，通过 evan-rag search 看结果；BM25 按词频、
   稀有程度与长度排序，而不是只数命中几个关键词。
3. 同义表达（需真实 Embedding）：资料写“任职于示例科技”，问“你的工作单位是什么”。
   向量分支有机会召回字面不重合内容，但效果取决于模型和阈值，不能保证任何同义词都命中。
4. 修改／删除：更新资料后旧向量失效，下一次查询增量生成；改为私有或归档后，访客不可查询。
5. 隔离：私人资料不会进入访客的候选集合或 Embedding 请求。返回前再次校验资料状态及更新时间。

## 限制

当前向量存储使用 SQLite JSON 数组缓存，并在可见候选上做精确余弦计算，不是大规模 ANN 引擎。
适合当前小型个人资料库；大规模时需要独立索引引擎和后台批处理任务。
首次索引可能较慢，默认每批超时 120 秒、每批 8 条（可通过 EMBEDDING_TIMEOUT_SECONDS 和 EMBEDDING_BATCH_SIZE 调整）；未配置或失败时回退 BM25。
阈值默认 0.3 仅为起始值，须用真实问题集校准。索引 fingerprint 绑定地址与模型名；
若供应商在相同模型名下更换权重或维度，应清理向量缓存后重建。
MMR 去重意味着一条长资料只返回最佳块；更多细节仍通过详情工具分页获取。
本次 77 项离线检查通过，语义测试使用替身向量，不代表真实 Embedding 模型已验收。

## 本地 bge-m3 启动

安装并打开 Ollama 后执行 `ollama pull bge-m3`。macOS 应用内的 CLI 也可直接使用：
`/Applications/Ollama.app/Contents/Resources/ollama`。

.env 使用以下配置（MiniMax 主模型配置继续保留）：

```dotenv
EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
EMBEDDING_MODEL=bge-m3
EMBEDDING_API_KEY=
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_BATCH_SIZE=8
```

在原 NO_PROXY 列表追加 `127.0.0.1,localhost`。打开 Ollama 应用保持服务运行，
然后执行 `uv run evan-api`。配置修改后需要重启已有 Agent 进程。
`uv run evan-rag status` 只检查配置；有可见已发布资料时执行
`uv run evan-rag search '你的工作单位是什么'`，确认 mode 为 hybrid。

使用的是 bge-m3 的 dense 向量（1024 维），搭配已有 BM25/RRF/MMR；
没有启用该模型的 sparse/ColBERT 输出，也没有新增神经重排模型。
模型文件由 Ollama 存在用户目录 ~/.ollama；原文和向量缓存仍在项目 .local 下。
本地 Embedding 请求不会把资料发送到第三方，但用于最终回答的检索内容仍会交给 MiniMax。

共享的 2GB 服务器建议调用独立的 Embedding 服务，不在该机器上加载模型。
迁移时修改 URL、模型标识和 Key；端点变化会生成新的索引 fingerprint，首次查询重新生成向量。
同名模型替换权重时应在停止 Agent 后清理向量缓存再重建，保留原文 knowledge.sqlite3。

参考：https://ollama.com/library/bge-m3 和 https://huggingface.co/BAAI/bge-m3。

## 可重复的真实问答评估

评估集位于 `evals/rag_cases.json`，包含 15 个独立问题和 4 个连续追问。
评分器位于 `evals/run_rag_eval.py`，直接使用当前公开资料、bge-m3 和 MiniMax，
按核心事实、禁止推断、引用来源、Top3 召回和知识库是否变化评分，不使用另一个模型裁判。

```bash
# 正常安装项目入口后运行三轮真实评估
uv run evan-eval --repeat 3

# 本地依赖已经安装、但暂时不希望 uv 联网同步时
uv run --no-sync python evals/run_rag_eval.py --repeat 3

# 修改评分规则后复用已有回答，不再调用模型
uv run --no-sync python evals/run_rag_eval.py --rescore
```

结果写入 `.local/evaluation/latest.json` 和 `latest.md`。`review_only` 问题会保留
回答，但在本人确认资料真实性前不计入准确率。每次运行只证明当前问题、资料和模型配置下
的表现；新增资料、更换模型或修改提示词后需要重新运行。

## 2026-11-02 真实评估后的改进

个人事实回答增加逐句证据约束，禁止以毕业日期推导入职时间、工作年限，
禁止把技术栈、爱好和就读经历扩写为熟练程度、习惯频率或学历层次。
访客提示不包含本人专用助手称呼。简单问题默认简短回答，减少无关扩写。
这些属于模型行为约束，不是确定性的事实验证器；仍需真实回归评估。

同时包含工作/职业经历与技术/技能的查询，以及简历、完整介绍查询，
检索器会按工作、技术、身份三个方向独立召回，与原问题结果轮流合并并按资料 ID 去重。
每个方向最多保留 11 条，沿用权限过滤与返回前版本校验；这是有限候选召回，不保证遍历全部资料。
元数据 query_strategy=multi_query 表示采用该策略。部分向量请求失败时标记 mixed。
该方案增加查询向量调用次数；没有增加神经重排模型，也没有自动改写旧资料。
