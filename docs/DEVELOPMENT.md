# AI 开发工作流

本流程参考本地 `aurora_front`（参考提交 `e7d453fe`）的 `AGENTS.md`、
`aurora-workflow-dev`、`09-tdd-workflow.md` 和 record/resume/doctor 脚本。
这里采用 Python 标准库实现，保留三档、紧凑记录、按需上下文、真实验证和恢复，并加入请求协议、故事线、最小方案及经验管理；
未复制 Memini 同步、前端领域 Skills、日报或多 Agent 编排。

## 一次任务只维护一条记录

入口是 [AGENTS.md](../AGENTS.md)，按 [架构地图](ARCHITECTURE.md) 定位代码。
使用 `docs/changes/<key>.json` 保存需求、范围、验收、决定、缺口、下一步与结果。
这些记录随代码审查和 Git 共享，只放脱敏摘要；完整日志与机器状态在被忽略的 `.workflow/`。
不同任务用不同 key，同一任务返工沿用 key；同一 key 只允许一个写入者，文件替换是原子的，但不提供多进程合并。
不把 AI 生成的规格当作新增业务授权，不需要为每个阶段反复请求许可。

新任务的完整步骤、请求分类、故事线／经验格式与渐进加载见 [请求与上下文协议](WORKFLOW_PROTOCOL.md)。
AI 负责识别请求并执行命令，用户正常提需求即可。以下基础命令继续兼容；新任务 intake 后按协议补 align、plan 和 link／exempt。

```bash
uv sync --locked
uv run python scripts/workflow.py intake example-change --request "修复一个已确认的问题" --scope "允许改变：目标边界；保留：其他既有行为" --acceptance "AC1 的可观察结果" --acceptance "受影响旧行为保持不变"
uv run python scripts/workflow.py resume example-change
```

默认 medium，可显式传 `--mode low` 或 `--mode full`。请求、范围、验收不能为空，key 不允许路径穿越，已有记录不会覆盖。
`acceptance` 是本项目的轻量规格：写清触发条件、预期结果和必要边界；新增逻辑可补充失败、重试、过期等实际相关场景。
AC 编号稳定，完成时必须逐项提供实现定位与实际验证证据。复杂任务仍在同一记录中补充决定和范围，不额外复制多个计划。
基础记录使用 v2；align 增加独立版本的 protocol 扩展。旧 v1 记录保持可读，主动修改或验证该任务时才升级并将旧内容存入 history，不批量改写历史。

每条 AC 在验证前通过 `map` 写入来源与完整 unittest ID（模块.类.方法），可重复 `--test` 关联多个用例：

```bash
uv run python scripts/workflow.py map example-change --ac AC1 --source "本次用户需求中的验收条款" --test test_auth.AuthTests.test_short_password_and_overwrite_rejected
uv run python scripts/workflow.py map example-change --ac AC2 --source "必须保留的旧行为" --test test_workflow.WorkflowTests.test_completion_requires_all_criteria_and_never_accepts_for_user
```

上述测试 ID 仅演示命令格式，实际任务须选能验证对应条件的用例；脚本不证明断言质量或语义覆盖。
每次 map 替换该 AC 的映射，并使本任务旧证据失效。全部映射确定后再验证，不将代码行为改成人工检查以规避测试。

跨阶段或交接时：

```bash
uv run python scripts/workflow.py progress example-change --next "运行相邻契约回归" --decision "按当前需求保留原默认值" --gap "尚未执行真实模型联调"
uv run python scripts/workflow.py progress example-change --next "验证第一条链路" --milestones "补齐失败路径" "运行回归"
uv run python scripts/workflow.py resume example-change
```

`progress` 保留当前请求与决定，重新打开任务并清除当前验证、人工观察和交付证据；未传 milestones 时保留，显式 `--milestones` 不带内容则清空。
无 key 的 resume 只在恰好一个活动记录时成功；不会猜选最近任务，也不自动恢复暂停／取消任务。协议任务默认只输出当前摘要，旧任务保持原输出；`--full` 显式展开历史，`--summary` 可为旧任务生成摘要。

需求纠正或返工用同一个 key，提供当前请求、范围、变更来源和**全部仍有效的 AC**：

```bash
uv run python scripts/workflow.py rework example-change --request "用户纠正后的请求" --scope "允许改变：新范围；必须保留：已确认边界" --source "本轮用户纠正的脱敏摘要" --acceptance "AC1=修改后的可观察结果" --acceptance "AC2=受影响旧行为保持不变" --milestones "调整实现" "重新验证"
```

rework 增加 revision，在同一 history 中保存上一版请求、范围、映射、结果及验证。相同编号与相同条件保留映射；条件改变则清空映射；未重列的 AC 标为 withdrawn。新增 AC 使用大于历史最大值的编号，禁止复用已撤回编号。
旧决定和缺口默认放在历史的 pending_review 中，不作为当前约定；核对当前用户口径仍有效后才加 `--retain-decisions` / `--retain-gaps`。重新明确 scope，不能靠 AI 自己修订记录扩展授权。
所有旧通过证据和用户验收均失效，剩余里程碑重新列出；原始文件基线不会重置。恢复时读当前约定和必要历史，核对 diff 后续接。

## TDD 与验证分级

- 业务规则、状态转换、权限和可复现缺陷优先先写失败测试，再实现、再按需重构。
- 红状态必须是预期行为断言失败，不能以导入失败、缺依赖或网络故障代替。
- 测试通过时先检查是否覆盖目标，不破坏正确代码制造失败。
- 文档、文案或不可自动化的真实交互使用适合的验证；不强制伪造红绿循环或拆成两次提交。
- 测试通过不证明模型真实输出正确；模拟链路与真实接口联调分别报告。

```bash
# 最小复现／红绿循环；不会自动写任务验证状态
uv run python scripts/run_tests.py test_auth.AuthTests.test_short_password_and_overwrite_rejected
# 上下文入口、记录结构、敏感路径跟踪和忽略规则检查
uv run python scripts/workflow.py doctor
# medium：静态检查 + 选定测试，不能据此宣称全量通过
uv run python scripts/check.py --record example-change --test test_auth test_sessions
# 默认／full：静态检查 + 全量测试，与 CI 同一入口
uv run python scripts/check.py --record example-change
```

`run_tests.py` 使用已有 unittest，零用例和无效测试名称都会失败。
`check.py` 无自动修复、无提交操作，300 秒单项超时；输出命令、退出码、时间、平台、输入指纹、测试数量和逐用例结果到 `.workflow/check.json`。
测试运行器支持 `--report PATH`，记录真实的 passed/failed/error/skipped 等结果与跳过原因；check 每次使用新的临时报告，缺失或无效报告不能复用旧成功结果。
使用 `--record` 同时回写该记录：保留全局数量、映射用例和未通过用例的详情，省略无关成功用例以保持紧凑。
AC 根据实际运行的完整用例 ID 判定：漏跑为 not_run，跳过为 skipped，失败为 failed，均不能 completion。未映射的 AC 也不能完成；无关平台用例的跳过保留在报告中，不冒充通过。
medium 聚焦测试必须覆盖该任务所有映射用例；full 不接受聚焦测试。输入指纹覆盖 Git 跟踪及未忽略的新文件，包括 JSON 夹具与暂存区状态；排除敏感路径、运行产物和任务 JSON（当前任务契约另有指纹）。验证中或验证后输入变化会使证据失效。
指纹是验证时输入的摘要，不能代替审查，也不是防篡改签名；记录可人工编辑，自动化不能证明语义或测试质量。

## 纯文档与人工检查

人工检查需要方法和实际观察，两者都保存在 AC 中。先完成全部 map 和文件修改，再执行检查、用 observe 留证，然后运行 check。也可同时关联自动测试与人工方法，两者都通过才能满足该 AC；observe 不是用户验收。

```bash
uv run python scripts/workflow.py intake docs-change --mode low --request "更新开发文档示例" --scope "只修改文档示例" --acceptance "示例与当前 CLI 参数一致"
uv run python scripts/workflow.py map docs-change --ac AC1 --source "本次文档需求" --manual "逐项对照 CLI --help 核对示例"
# 完成文档修改并实际核对后，填写观察结果；失败或阻塞可加 --status failed|blocked
uv run python scripts/workflow.py observe docs-change --ac AC1 --evidence "已对照 CLI 帮助核对命令和参数，示例一致"
uv run python scripts/check.py --docs-only --record docs-change
uv run python scripts/workflow.py completion docs-change --result "文档示例已更新" --evidence "AC1=人工参数核对与 doctor 通过"
```

intake 将起始公开文件的内容哈希与 Git 索引状态保存到忽略的 `.workflow/baselines/<key>.json`，记录仅保存其摘要。只比较任务开始后变化，避免把起始已有改动归到本任务；这不证明起始代码正确。
`--docs-only` 必须指定 low 任务。允许根目录或 docs/ 下的 `.md` / `.rst` 及 PR 模板；代码、配置、删除、未知文件和暂存区变化均参与判断，不能只靠扩展名或档位放行。
基线丢失、损坏、跨机器不可用或旧任务没有基线时，拒绝 docs-only，改用完整 check；不要补造或重置基线。人工观察同样绑定输入和契约指纹，后续变化后必须重新观察。

静态检查先启用 Ruff 的 E9/F63/F7/F82：语法与高置信度错误。没有宣称完成全量风格规范或静态类型检查，避免借工作流接入重写现有代码。
不默认生成覆盖率百分比；验收关注行为和边界。后续按实际需要增加规则。

## 完成与恢复

```bash
uv run python scripts/workflow.py completion example-change --result "本次实现摘要" --evidence "AC1=实现位置；执行命令；观察到的结果" --evidence "AC2=旧行为回归结果及未验证项"
```

completion 重新运行轻量 doctor，检查最新验证是否通过、输入和任务契约是否变化、每条 AC 的实际映射结果和证据是否完整；文档任务再次核对变更范围。完成后清空剩余里程碑，不会把用户验收改为 accepted。
记录里的 `implementation`、`verification`、`acceptance_status` 分别表达实现、自动验证和用户确认。
用户明确验收后，才可人工更新 acceptance_status，并写入 user_confirmation 的脱敏原声及来源。
档位仍按实际风险选择；脚本防止机械性漏跑和范围误用，无法证明需求理解、测试语义或人工观察正确。部分 AC 未通过时保持 in_progress 并报告缺口，不修改条件来伪造完成。
手工修改记录时不能伪造命令或代码指纹。未验证范围保留在 gaps 中。

完成后自审 diff，核对允许改变的内容与必须保留的行为。PR 使用 [模板](../.github/pull_request_template.md)，关联这条记录即可。
可复用经验维护到 `docs/experience/`，注明证据、适用条件、反例和替代关系；全局工程约定仍维护到本文件或架构地图，一次性操作不增加全局规则。

## CI 与可复现环境

[CI](../.github/workflows/ci.yml) 在 PR、main 推送和手动触发时运行 Windows／Linux 与 Python 3.11／3.12 矩阵：
锁文件同步 → 同一 check 命令 → 包构建。无需配置模型密钥，不启动真实 Agent。
本地 `uv sync --locked` 默认包含 dev 组；只部署运行依赖时用 `uv sync --locked --no-dev`。
CI 不自动部署、合并或设置远端分支保护；仓库维护者可将检查设为合并必需项。

POSIX `0600` 权限断言独立成平台测试：Linux 执行，Windows 明确 skip，其余认证和资料行为在两平台执行。
这解决测试语义的跨平台差异，没有实现或验证 Windows ACL 隔离。

## 仓库卫生与既有边界

doctor 只检查 Git 索引中的路径名，不读取敏感内容：禁止跟踪 `.env`、其他 `.env.*`（保留 `.env.example`）、
`.local/`、`.workflow/`、`.venv/`、数据库及其 journal/WAL 文件。规则覆盖嵌套目录和大小写；这是路径门禁，不是任意文件内容的密钥扫描器。
不能通过增加豁免清单将真实个人数据当测试样例，测试使用临时生成的数据。

本次接入将已跟踪的本机配置和运行数据从索引移除，文件仍留在本地；历史提交仍可能包含这些文件。
密钥轮换和 Git 历史清理需要单独执行，本工作流不声称已经完成。
既有业务差异（例如密码长度说明、记忆审批默认值）不在工作流接入范围内；后续按独立需求处理。

## 来源与后续维护

- Aurora 的“五段闭环”映射为理解、对齐、实施、验证、交付；工作记录同时承载轻量 SDD。
- 故事线通过版本化 JSON 场景与 AC 关联，按公开源码／测试记录当前事实；不引入图编辑器或服务端状态。
- resume 从同一 Git 记录派生当前摘要，完整历史按需展开；不同机器共享规格，本地检查报告和上下文索引保留在 `.workflow/`。
- 使用 GitHub 官方 [checkout](https://github.com/actions/checkout) 与 [setup-uv](https://github.com/astral-sh/setup-uv) 动作；更新依赖和动作时验证锁文件与 CI。
