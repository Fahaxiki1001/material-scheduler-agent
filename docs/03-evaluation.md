# 验收、实际结果与限制

## 当前结论

在 Python 3.12.10 下完成 **87 个自动测试，通过 87 个**；升级后离线与 DeepSeek Flash 真实模式验收均为 **14/14**。受控真实自纠探针通过。历史首轮 10/12、第二轮 12/12 及升级过程的常规真实评估记录保留。

真实模型记录包含正常完成、工具失败、规则阻断、冲突后补查、请求确认与授权窗口内改排。请求缺少时间的场景由输入校验直接结束，未调用模型。Mock 结果仅证明工程流程，真实决策证据以 real 目录为准。

## 运行命令与原始证据

在项目根目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m unittest discover -s tests -v
python3 -m scheduler_agent evaluate --mode mock --output runs/review-mock
python3 -m scheduler_agent evaluate --mode real --output runs/review-real
```

归档证据仍位于下面列出的 examples 目录；以上复现命令将新结果写入 runs，不覆盖归档。重复复现时请更换新的输出目录，程序拒绝覆盖非空目录。真实模式需要评审者自行配置密钥，离线模式无需配置。

验证分为三层：自动测试验证工具、事务、Runner 和协议；Mock 场景验证确定性流程；real 场景使用实际 DeepSeek 模型决定动作，但业务工具仍是本地函数、SQLite 与合成资料，并通过指定故障注入覆盖失败。不声称接入了真实审核或发布后台。

- 自动测试原始输出：[examples/test-results.txt](../examples/test-results.txt)。
- 最终真实评估报告：[deepseek-upgraded-03/report.json](../examples/real/deepseek-upgraded-03/report.json)。
- 首轮真实评估报告及失败：[deepseek-run-01/report.json](../examples/real/deepseek-run-01/report.json)。
- 最终离线评估报告：[examples/mock/upgraded-03/report.json](../examples/mock/upgraded-03/report.json)。
- 提交材料保留最终 Mock 与最终受控探针，重复的旧轮次已清理；常规真实评估的失败、成功与改进记录保留。最终结果以 upgraded-03 目录为准。
- 受控真实自纠探针：[report.json](../examples/real/finish-recovery-probe-03/report.json)、[Trace](../examples/real/finish-recovery-probe-03/trace.jsonl)。

下表中的每个场景目录都包含 `input.json`、`output-1.json`、`trace-1.jsonl`、`database-snapshot.json` 和实际使用的数据副本 `data/`。duplicate 额外包含第二次输出与 Trace。

## 六个必须验收点

| 验收点 | 实际场景与结果 | 证据 |
| --- | --- | --- |
| 正常排期 | completed / saved，草稿回执与数据库一致 | [normal/output-1.json](../examples/real/deepseek-upgraded-03/normal/output-1.json)、[Trace](../examples/real/deepseek-upgraded-03/normal/trace-1.jsonl) |
| 素材信息缺失 | needs_input，缺 rights_status，数据库 0 行 | [missing_info/output-1.json](../examples/real/deepseek-upgraded-03/missing_info/output-1.json)、[Trace](../examples/real/deepseek-upgraded-03/missing_info/trace-1.jsonl) |
| 规则风险 | blocked，包含 R01 授权到期原因，数据库 0 行 | [blocked_rule/output-1.json](../examples/real/deepseek-upgraded-03/blocked_rule/output-1.json)、[Trace](../examples/real/deepseek-upgraded-03/blocked_rule/trace-1.jsonl) |
| 时段冲突 | 先观察 18:00 冲突，再补查候选并选择替代；needs_confirmation，未保存 | [hard_conflict/output-1.json](../examples/real/deepseek-upgraded-03/hard_conflict/output-1.json)、[Trace](../examples/real/deepseek-upgraded-03/hard_conflict/trace-1.jsonl) |
| 重复提交 | 第二次 already_exists，两次同一 draft_id，数据库 1 行 | [duplicate/output-1.json](../examples/real/deepseek-upgraded-03/duplicate/output-1.json)、[output-2.json](../examples/real/deepseek-upgraded-03/duplicate/output-2.json)、[第二次 Trace](../examples/real/deepseek-upgraded-03/duplicate/trace-2.jsonl) |
| 保存失败 | failed / SAVE_FAILED，提交前故障回滚，数据库 0 行 | [save_failure/output-1.json](../examples/real/deepseek-upgraded-03/save_failure/output-1.json)、[Trace](../examples/real/deepseek-upgraded-03/save_failure/trace-1.jsonl) |

## 八个补充场景

| 场景 | 实际结果 | 证据 |
| --- | --- | --- |
| 用户明确确认替代时段 | 19:00 草稿保存成功 | [confirmed_alternative/output-1.json](../examples/real/deepseek-upgraded-03/confirmed_alternative/output-1.json) |
| 读取失败后恢复 | 一次 READ_FAILED，观察结果后重试，最终完成 | [read_failure_recovery/trace-1.jsonl](../examples/real/deepseek-upgraded-03/read_failure_recovery/trace-1.jsonl) |
| 步数上限 | 两次模型回合后 incomplete，原因 step_limit_reached | [step_limit/output-1.json](../examples/real/deepseek-upgraded-03/step_limit/output-1.json) |
| 请求缺少时间 | needs_input，未调用模型和业务工具 | [missing_request/output-1.json](../examples/real/deepseek-upgraded-03/missing_request/output-1.json) |
| 素材需人工复核 | needs_confirmation，未保存 | [manual_review/output-1.json](../examples/real/deepseek-upgraded-03/manual_review/output-1.json) |
| 改变已有占用 | 观察占用后选择其他授权时段，保存结果随之变化 | [changed_occupancy/trace-1.jsonl](../examples/real/deepseek-upgraded-03/changed_occupancy/trace-1.jsonl) |
| 授权窗口已满 | 窗口内候选均占用，blocked / no_feasible_slot | [no_slot_in_window/trace-1.jsonl](../examples/real/deepseek-upgraded-03/no_slot_in_window/trace-1.jsonl) |
| 没有完整候选 | 授权窗口内放不下任一固定时段，明确 blocked | [empty_candidate_window/trace-1.jsonl](../examples/real/deepseek-upgraded-03/empty_candidate_window/trace-1.jsonl) |

## 自动测试重点

- 真实 SQLite 事务插入后故障回滚、幂等重放、同键不同内容拒绝。
- 两个不同请求同时竞争同一时段，只有一份成功；两个同内容重复请求返回相同草稿 ID。
- 查询后被另一请求抢占，保存时再次检查发现冲突。
- 相邻时段不重叠；与已有排期部分重叠时阻断。
- 未先审核直接调用保存，也无法绕过风险检查。
- 模型额外传 confirmed 或 request_id 参数被拒绝；用户确认只能授权精确时段。
- 已查询多个空闲候选时，保留模型明确选定的建议；未经查询的建议不能被标成可用。
- 无保存回执却声明 completed，会收到 FINISH_REJECTED 并可在剩余预算内补齐工作；持续错误或预算耗尽时明确未完成。
- 保存失败后发生其他工具错误仍保留独立保存状态；只查询授权窗口内候选即可判断无可行时段。
- 模型多工具批次、非法 JSON、调用超限、模型错误和证据覆盖保护。
- HTTP 请求和工具返回协议、重定向凭证保护、错误内容不泄漏、环境变量优先级。
- 独立 Python 子进程按 README 的 CLI 入口完成保存、重复执行和故障退出码验证。

测试代码分别位于 `tests/test_tools.py`、`tests/test_runner.py`、`tests/test_model_client.py`、`tests/test_cli.py`、`tests/test_evaluation.py`。网络协议测试使用响应替身，没有对真实模型发送请求。

## 真实模型路径与边界

真实 normal 路径为读取 → 审核 → 查询指定时段 → 保存 → 结束。输出回执与实际数据库快照一致。

真实 hard_conflict 路径为读取 → 审核 → 查询原时段并观察冲突 → 补查其他候选 → 明确选择一个可用替代 → 请求确认，数据库无新增草稿。

真实 changed_occupancy 路径在观察原时段冲突和其他候选可用性后，选择授权窗口内的替代时段并保存。允许一次查询多个候选，也允许后续补查；评估要求动作发生在观察之后，不要求固定查询次数。路径间的不同动作来自真实模型响应，Runner 只分发调用与核验边界。

离线策略替身内部仍是固定条件分支，不作为上述真实决策证据。真实模型参数为非思考模式、temperature=0；一次完整通过不能保证未来每次运行都完全一致。

## 真实模型复现

提交仓库不含真实密钥。按 README 配置自己的 .env 后，在项目根目录使用新输出目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m scheduler_agent evaluate --mode real --output runs/review-real-new
```

使用同一场景集检查模型实际行为。冲突场景必须有原时段冲突、所选候选可用以及观察之后调整动作的证据。允许一次查询多个候选，不强制再次查询；模型声明状态也必须得到工具记录支持。正常与冲突路径至少应形成两条真实运行记录。

若失败，应保留实际文件并记录原因，不能通过改写输出或把 Mock 目录改名来补齐。不同供应商的 strict schema 与工具调用参数兼容性也须实测。

## 首轮失败与改进

首轮 hard_conflict 在首次模型请求/协议阶段失败。旧日志仅记录通用模型错误，无法事后判断具体 HTTP 状态或响应格式，因此不把它归因为某个确定的服务端问题。之后增加了安全错误分类，保留状态码，不记录密钥或原始错误正文。

首轮 changed_occupancy 已观察冲突、补查到空闲候选，但模型把已授权 flexible 窗口误认为仍需确认。核验层没有伪报完成，而是返回 incomplete。明确“授权窗口内调整无须再次确认，应保存草稿”并将采样温度设为 0 后，第二轮通过同一断言集。没有放宽确认或数据库验收条件。

接入前的单例失败还暴露出本地 Python 缺少默认 CA 路径；系统 CA 可验证同一官方端点。客户端在满足条件时使用 macOS 系统 CA，始终开启证书与主机名校验。初次连接记录保存在 `examples/real/connection-diagnostics/`。

## 升级复测与评估修正

改造后的首轮为 [14/14](../examples/real/deepseek-upgraded-01/report.json)，保留了一次自然触发的结束自纠记录。随后统一错误反馈中的候选范围，第二轮为 [12/14](../examples/real/deepseek-upgraded-02/report.json)：

- changed_occupancy 实际完成合法改排，但旧断言强制要求第二次查询，误判了“一次查询多个候选后调整”的合法路径。
- empty_candidate_window 在已知候选集合为空后又尝试无效时间，普通工具错误盖过了业务上已知的无可行方案。

评估改为按观察与动作的先后关系核对证据，并增加缺少冲突观察、先保存后观察等反例测试；运行时让已证实的无可行方案优先于普通参数错误，但不能覆盖实际保存故障。最终第三轮 [14/14](../examples/real/deepseek-upgraded-03/report.json) 通过。没有改变硬性授权、数据库回执或风险边界。

[受控探针](../examples/real/finish-recovery-probe-03/report.json) 的首轮提前结束由明确测试指令诱导，模型在收到真实 FINISH_REJECTED 后补齐业务并成功保存。该探针不代表自然错误率，指令与条件完整留档。详见 [改造说明](07-state-and-finish-recovery.md)。

## 限制

当前模型与合成场景下已完成真实服务兼容性与决策验证；未进行大样本稳定性评估，只记录服务返回的 token 用量，不推算实际费用或失败请求计费。审核只涉及合成元数据，时段/渠道规则刻意收窄；未实现生产发布、真实内容审核、多用户确认身份与跨进程恢复。安装包的联网构建未在当前环境验证，直接源码运行和 CLI 子进程运行已验证。
