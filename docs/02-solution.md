# 素材审核与发布排期 Agent：实现方案

状态：已实现并接入 DeepSeek Flash，87 个自动测试通过，最新真实验收 14/14 通过，受控真实自纠探针通过。历史失败与改进证据保留。详情见 `03-evaluation.md` 与 `07-state-and-finish-recovery.md`。

本文保留最初约 3 小时的范围设计，以下按最终实现同步接口。运行与自动测试均使用 Python 标准库；合成数据放在包内以支持安装后读取；`finish` 是四个业务工具之外的结束控制动作。

## 1. 目标与范围

为内容运营读取素材、检查规则、查询时段，生成无冲突的本地排期草稿。模型须观察工具返回结果，自主选择下一步工具、补查、请求补充或结束。

核心交付采用 Python CLI、合成 JSON 数据、SQLite、本地业务工具和可替换模型适配器。不做前端、真实发布、账号系统、图片/视频内容识别、向量数据库、多 Agent 或复杂框架。审核仅针对合成元数据与明确规则，不声称具备真实内容安全审核能力。

首版支持一个渠道、固定 30 分钟发布时段；单次输入一个素材。多素材可由 CLI 外层逐项调用同一 Agent，各项共享数据库。保留同一日、同一渠道的候选时段，不自动跨日或改变渠道。

替代时段由模型通过 `finish.suggested_start` 明确选择，最终输出只接受已查询、可用且未越界的候选。结果组装代码不默认挑选第一个空闲时段。`finish` 的 suggested_start 在无建议时为 null。

## 2. 架构与职责

```text
CLI 输入与配置
    ↓
Request + Tools：原始约束、用户确认、request_id、运行状态
    ↓
Agent Runner ←→ 模型适配器（真实模型 / 离线脚本模型）
    ↓ 模型选择工具和参数
工具分发与校验
    ├─ read_material
    ├─ check_rules
    ├─ query_slots
    └─ save_draft → SQLite
    ↓ 工具结果回传模型，继续决策或结束
可信执行账本 → 最终结构化结果 + 人类可读摘要
    └─ 每次运行生成 JSONL Trace
```

Runner 只实现通用工具循环、参数验证、预算与错误处理，不根据素材风险预先编排整条业务路线。模型决定读取什么、是否重查、选择哪些候选时段以及何时结束。

确定性代码负责底线：禁止越权改变硬性约束、禁止有阻断风险时保存、禁止时段冲突与重复创建、禁止未提交成功却报告已保存。这些守卫不替代模型规划。

模型接入不绑定供应商。适配器定义 `complete(history, tool_schemas)`，通过 Chat Completions 工具调用协议返回统一消息；用环境变量配置模型、服务地址与凭证。使用标准库 HTTP 客户端，无模型 SDK 或运行时第三方依赖。密钥不进入仓库、Trace 或示例文件。DeepSeek 官方服务已实测；其他兼容服务仅有通用协议适配，不承诺未经实测的供应商兼容性。

项目版本为 0.1.0，Python 最低声明版本为 3.11，实际验证环境为 3.12.10。未使用外部 Agent 框架，因此没有需要声明的框架版本；Runner、工具分发、状态与日志职责均由本仓库源码实现。替代方案与本次取舍：工作流框架便于复杂图与恢复，但本题单 Agent、四个工具的范围优先采用容易检查的原生循环；前端和部署未纳入范围。

当前已配置并实际验证 DeepSeek 官方 `deepseek-flash` 和 `https://api.deepseek.com`。该标准端点适配显式关闭思考模式以支持 required 工具选择，temperature=0，不发送 Beta strict 标记或 parallel_tool_calls；仍保留完整本地参数校验和每回合单工具守卫。每次输出上限为 2048 tokens。通用适配器的原协议不变。

## 3. 输入与业务口径

输入示例：

```json
{
  "request_id": "req-demo-001",
  "material_id": "M01",
  "channel": "douyin",
  "requested_start": "2026-10-29T18:00:00+08:00",
  "time_constraint": "hard",
  "approved_alternative": null
}
```

- 时区固定 Asia/Shanghai，所有持久化时间包含时区偏移。
- 时段使用半开区间 `[start, end)`；18:00–18:30 与 18:30–19:00 不冲突。
- `request_id` 是输入格式层必填项，缺失或非法时 CLI 报错并退出 2；素材 ID、渠道或发布时间等业务信息缺失时返回 `needs_input` 和缺失项。非法日期等格式错误由 CLI 明确报错。
- `time_constraint=hard` 时，模型只能建议替代时段。外部用户输入提供与候选起止时间一致的 `approved_alternative` 后，才允许保存替代方案。
- `time_constraint=flexible` 表示用户已授权在同一日、同一渠道的候选时段内调整。必须同时提供允许窗口，保存前再次验证。
- 用户确认从 CLI 输入进入只读 Request，模型工具参数中不提供 `confirmed=true` 等自行授权入口。
- 正常时段符合原始要求时，可以直接保存草稿；不增加题目未要求的额外确认。
- 有警示但不阻断的素材须在输出保留风险说明；明确需人工审核的规则，返回待确认或阻断，不自动豁免。
- 保存后的草稿在本地排期中占位，后续请求会查到该占位；所有状态始终称为“草稿”，不称为“已发布”。

输入字段约定：

| 字段 | 类型与用途 |
| --- | --- |
| request_id | 非空字符串，幂等标识；同一已保存请求改变内容需换新编号 |
| material_id、channel | 素材与渠道；业务上必需，缺失时请求补充 |
| requested_start | 带时区的 ISO 8601 时间；当前支持整点或半点起始 |
| time_constraint | hard 或 flexible，默认 hard |
| allowed_window | start、end 对象；flexible 时必需，候选时段须完整落入窗口 |
| approved_alternative | start、end 对象或 null；精确确认硬性要求的替代时段 |
| human_review_approved | 布尔值，默认 false；仅表示用户确认人工复核，不可豁免阻断风险 |

## 4. 四个工具

以下签名省略公共的 reason 参数；实际工具 schema 要求每次提供一句简短动作依据，记录在 Trace 中，不要求模型内部推理。

所有工具返回统一信封：

```json
{
  "ok": true,
  "data": {},
  "error": null
}
```

失败示例：`{"ok": false, "data": null, "error": {"code": "SAVE_FAILED", "message": "草稿写入失败", "retryable": false}}`。工具异常转为结构化结果回传模型；不中途伪造成功。

### 4.1 read_material(material_id)

返回素材 ID、版本、标题、渠道、授权状态、缺失字段，以及该请求已存在的草稿摘要（如有）。request_id 由 Runner 的 Request 绑定，模型不能覆盖。

合成素材只保存评估所需字段，例如 `title`、`channel`、`rights_status`、`review_status`。资料中的自由文本只当作业务数据，不允许覆盖工具权限与用户约束。

### 4.2 check_rules(material_id)

读取可信数据源执行规则，模型不提交自己声称的审核结果。

返回 `audit_status=pass|warning|blocked|incomplete`、规则 ID、原因、需补充字段、素材版本与规则版本。规则示例：授权到期 → blocked；授权未知 → incomplete；需人工复核 → warning + requires_confirmation。

测试规则可直接断言，避免将审核质量完全依赖模型的主观判断。

### 4.3 query_slots(starts)

渠道与日期由原始请求绑定，模型只提交待查询的 `starts` 数组。返回每个时段的 start、end、是否可用、冲突来源、授权状态和允许继续查询的候选起点。模型可以先查 18:00，再依据返回结果补查其他时段。

不由工具直接决定最终排期。查询结果只是当前快照，最终保存仍会重新检查占用。

冲突公式：`new.start < existing.end and existing.start < new.end`。已有排期与其他请求的有效草稿都参与检查；同一请求已保存的同一份草稿不会被当成外部冲突。

### 4.4 save_draft(start, end)

请求、素材、渠道与确认信息从只读 Request 取值，模型只能提出待保存的时段。

保存前验证：

1. 校验起止时间是一个完整的 30 分钟时段。
2. 开启 SQLite 写事务，先检查幂等记录：同键同内容返回原草稿；同键不同内容返回 `IDEMPOTENCY_CONFLICT`。
3. 新建草稿时检查信息完整性，重新执行本次数据快照上的规则检查，校验人工复核、用户时间授权和候选表。
4. 在同一事务中重新读取数据库占用，确认没有冲突后插入草稿并提交。
5. 只有提交成功才返回 `saved`；写入后提交前的模拟故障回滚并返回 `SAVE_FAILED`，其他数据库异常也明确返回失败。

素材和规则来自启动时加载的合成数据快照，不做文件热更新；“重新检查审核”表示再次执行该快照上的规则。排期占用则在保存事务内从数据库重新读取。两者职责不同。

工具返回 `save_status=saved|already_exists`、draft_id、实际保存时段与关联版本。守卫失败返回明确错误码，例如 `CONFIRMATION_REQUIRED`、`RULE_BLOCKED`、`SLOT_CONFLICT`。

幂等检查在一次性故障注入与新建逻辑之前执行，已成功的同内容重复请求仍可返回原草稿。幂等重放只能证明原草稿存在，不声称已按当前规则重新审核。

## 5. 数据与状态

合成数据位于 `src/scheduler_agent/data/`：`materials.json`、`rules.json`、`existing_schedule.json`、`candidate_slots.json`。数据显式包含版本，便于替换素材或时段验证行为是否变化。

SQLite 最小表：

```text
drafts
  draft_id             主键
  request_id
  material_id
  intent_hash          原始约束与用户授权的规范化摘要
  payload_hash         实际草稿内容摘要
  channel
  start_at / end_at
  material_version / rules_version
  audit_json           原保存时的审核快照
  created_at
  UNIQUE(request_id, material_id)
```

不依赖唯一键防止重叠时段：保存使用短写事务串行化“检查占用 → 写入”，并在事务中判断区间重叠。不同请求同时竞争同一时段时，只允许一份成功。

运行状态采用独立维度：

- `task_status`：completed / needs_input / needs_confirmation / blocked / failed / incomplete。
- `audit_status`：unknown / pass / warning / blocked / incomplete。
- `save_status`：not_attempted / saved / already_exists / failed。

例如：替代时段尚未确认 → needs_confirmation + pass + not_attempted；保存报错 → failed + pass + failed；工具调用预算耗尽 → incomplete。

最终输出包含原要求、建议时段、风险、缺失项、待确认项、草稿 ID、保存状态、独立 save_error 和 Trace 路径。保存状态与草稿 ID 必须来自可信工具结果。模型的结束建议若与账本矛盾，先返回 FINISH_REJECTED 让模型在预算内自纠；仍不一致或预算耗尽时返回未完成，不采信虚构的成功。

## 6. Agent 循环与运行记录

```python
for turn in range(max_model_turns):
    action = model.complete(history, tool_schemas)
    if action.tool_name == "finish":
        candidate = finalize_from_verified_evidence(action, ledger)
        if candidate.status_matches:
            return candidate
        result = finish_rejected_feedback(candidate)  # 回传事实，不代替模型规划
    else:
        result = validate_and_execute(action, readonly_context)
    ledger.append(action, result)
    trace.write(action, result)
    history.append_tool_result(action, result)
return incomplete(reason="step_limit_reached")
```

默认限制：最多 8 次模型决策、12 次调用（含结束动作）；无效工具名或非法参数也消耗调用预算。首版只允许每个模型回合一个工具调用，降低状态与日志复杂度。工具失败交由模型选择重试、补查或结束；自动重试本身不计为 Agent 决策证据。

真实模型服务不可用时返回明确错误，并提供显式 `--mode mock`。离线脚本模型只验证 Runner、工具、守卫和日志流程，不能作为真实模型动态决策达标证据。Trace 必须标明 `model_mode`，不混用两类结果。

Trace 使用 JSONL，每步记录：run_id、step、model_mode、动作、简要动作理由（如模型实际提供）、工具参数、返回摘要、错误码、时段/版本/草稿 ID、耗时。下一步动作自然出现在后续事件中。记录开始、结束、模型错误与预算耗尽事件。

不要求或保存模型内部思维过程，不补写模型未提供的理由。运行输出由日志直接生成，不手工编造。

## 7. 验收与评估

| 用例 | 条件 | 预期结果 | 验证重点 |
| --- | --- | --- | --- |
| normal | M01 合规，19:00 空闲 | completed + saved | 实际时段与数据库一致 |
| missing_info | 素材缺授权信息 | needs_input，不保存 | 具体缺失字段 |
| blocked_rule | 授权到期 | blocked，不保存 | 风险原因与规则 ID |
| hard_conflict | 硬性 18:00 占用，19:00 可用 | 查询替代并 needs_confirmation，不保存 | 不擅自改变硬性要求 |
| confirmed_alternative | 用户明确同意 19:00 | completed + saved | 确认与时段绑定 |
| duplicate | 相同请求连续执行两次 | 同一 draft_id，只有一行 | 幂等与重复成功语义 |
| save_failure | 新请求保存时注入异常 | failed，无新增行 | 回滚与真实失败状态 |
| exhausted | 达到调用上限 | incomplete | 有界执行 |
| race | 不同请求竞争同一时段 | 只有一个保存成功 | 事务内冲突检查 |
| changed_occupancy | 将空闲候选改为占用 | 模型补查、请求确认或停止 | 动作确实依工具结果改变 |

另外验证：原始请求缺发布时间、模型伪造确认、模型未检查就尝试保存、同幂等键修改请求内容、连续相邻时段、模型服务失败。守卫测试可以直接调用工具，避免依赖模型是否会碰巧产生违规动作。

测试分两层：

1. 可离线自动执行的工具/事务/Runner 测试，用可控输入验证工程性质。
2. 真实模型端到端评估，读取实际 Trace 和数据库断言结果。至少保存两条不同工具结果导致不同动作的真实路径，建议保存正常、冲突调整、保存失败三类及所有验收用例。

真实模型评估不锁死措辞或唯一工具顺序，只验证业务状态、硬性边界和实际动作变化，并检查模型所声明状态是否被可信账本支持。若模型没有执行预期重规划，保留失败记录，调整后重新运行并如实说明。不能通过预设日志替代模型运行。最初两轮真实评估保存在 `examples/real/deepseek-run-01/` 和 `deepseek-run-02/`；当前最终升级报告在 `examples/real/deepseek-upgraded-03/`，受控探针在 `examples/real/finish-recovery-probe-03/`。

## 8. 当前主要目录与命令

```text
README.md
pyproject.toml
.env.example
src/scheduler_agent/
  cli.py
  agent.py
  models.py
  model_client.py
  tools.py
  storage.py
  trace.py
  provenance.py
  evaluation.py
  data/
    materials.json
    rules.json
    existing_schedule.json
    candidate_slots.json
tests/
  test_tools.py
  test_runner.py
  test_model_client.py
  test_cli.py
  test_evaluation.py
examples/
  run_finish_probe.py
  test-results.txt
  inputs/
  real/
  mock/
docs/
  01-requirements.md
  02-solution.md
  03-evaluation.md
  04-ai-usage.md
  05-retrospective.md
  06-deepseek-setup.md
  07-state-and-finish-recovery.md
```

已实现的命令接口：

```bash
export PYTHONPATH="$PWD/src"
python3 -m scheduler_agent run --input examples/inputs/normal.json --mode real --db runs/demo.db
python3 -m scheduler_agent run --input examples/inputs/normal.json --mode mock --db runs/offline.db
python3 -m scheduler_agent evaluate --mode real --output examples/real/run-01
python3 -m unittest discover -s tests -v
```

评估程序为每个独立场景创建独立临时数据库；duplicate 场景两次执行共享数据库。并发竞争由 tests/test_tools.py 中的多线程测试覆盖，使用共享数据库，不计入 14 个模型场景。故障配置由测试启动参数控制，不暴露给模型作为普通业务工具参数；save_failure 注入一次提交前故障，read_failure_recovery 注入一次读取失败，正常运行默认关闭。

## 9. 初始实施预算（计划，非实际工时）

下表为最初约 3 小时的计划，不是实际分项耗时。候选人提供的实际投入约为 2 小时，记录在 05-retrospective.md。

| 时间 | 产出 |
| --- | --- |
| 0–20 分钟 | 确定输入输出、合成数据、规则与验收预期 |
| 20–65 分钟 | 四个工具、数据库、确认守卫、幂等与回滚 |
| 65–100 分钟 | 模型适配器、通用循环、调用上限与 Trace |
| 100–145 分钟 | 自动测试、真实模型运行、修复核心问题 |
| 145–180 分钟 | 整理示例、五份文档、全新数据库复现与提交检查 |

先打通一次真实模型的完整链路，再完善边界；时间不足时压缩抽象层与额外功能，保留全部必验场景。实际工时与未完成项如实记录。

## 10. 提交前检查

- README 从安装到运行可复制，支持明确的离线入口。
- 六个必验场景均有代码测试或真实评估证据，并在评估文档逐项引用。
- 至少三组输入、实际输出与 Trace，覆盖正常、资料不足和工具失败。
- 至少两条真实模型路径体现工具结果不同导致动作变化。
- 真实与 Mock 结果分开，失败和限制不隐藏。
- 不包含真实密钥或隐私数据；AI 使用范围与个人修改如实记录。
- 最终提交题号、GitHub URL 与最终 commit SHA。
