# 状态一致性与结束自纠改造

本次改造针对代码评审中复现的两个状态问题，并增加有上限的结束反馈循环。业务工具仍负责安全边界，模型仍负责选择下一步动作。

## 1. 保存状态不再依赖最新错误

原问题：一次保存返回 SAVE_FAILED 后，如果模型又触发其他工具的参数错误，最终 save_status 会错误显示 not_attempted。

改造后将持久化结果与普通工具错误分开记录：

- save_status 取自保存回执或独立的 save_error，不被最新工具错误覆盖。
- error 保留当前工具错误，save_error 独立保留保存故障；两者可以同时存在。
- 后续真正保存成功才清除 save_error；既有失败事件仍留在 Trace 中。

回归测试覆盖“保存失败 → 其他工具参数错误”和“保存失败 → 再次保存成功”，并检查数据库实际行数。

## 2. 无可行时段按用户授权范围判断

原问题：用户仅授权 19:00–19:30，该唯一候选已占用，但系统要求查询全天候选（含窗口外）才能报告无可行方案。

改造后 relevant_candidates 根据渠道、日期和用户授权计算有效候选。hard 的同日替代时段可作为待确认建议；flexible 的窗口外时段被排除。

query_slots 返回的后续 candidate_starts 也使用这个集合。判断全部占用只需覆盖有效集合；窗口内没有任何完整候选时段，也可在审核后明确返回 blocked / no_feasible_slot。

额外测试确保：只查到部分有效候选时，不能提前宣称无可行时段。

## 3. finish 被拒绝后回传事实，让模型继续决策

此前 finish 声明与执行账本不符时，程序修正最终状态并立即退出。现在先返回结构化的 FINISH_REJECTED，包含审核状态、保存状态、缺失项、待确认项、已观察时段和剩余预算；模型可依据这些事实补查、保存或改正结束状态。

```text
模型 finish completed，但没有保存回执
    → 核验拒绝，返回 FINISH_REJECTED
    → 模型观察反馈，选择读取、审核、查询或保存
    → 获得真实保存回执后再次 finish
    → 核验通过才结束
```

控制反馈不会覆盖业务错误或执行保存；没有预设下一项业务动作。FINISH_REJECTED 有独立事件，也作为工具观察回传模型。最终有效声明的 status_corrected 为 false；之前的拒绝次数单独记录为 finish_rejections。

默认最多拒绝 2 次结束：第一次拒绝后可继续，第二次拒绝时以 incomplete / finish_rejection_limit_reached 终止。所有结束尝试共用 8 次模型决策、12 次调用预算；调用或步数先耗尽时仍明确返回未完成。可用 --max-finish-rejections 配置拒绝上限。

## 4. 验收与运行证据增强

常规验收从 12 个扩展到 14 个，新增授权窗口已满、窗口内没有完整候选两个场景。断言还核对缺失字段、具体规则原因、待确认项、确认时段、保存授权范围和独立保存错误。

新的 Trace 包含：

- 运行时包内 Python 源文件、基础提示、工具定义和数据的 SHA-256 指纹；源码指纹不是 Git commit SHA。
- Python 版本、结束拒绝上限、finish_rejected 事件。
- 服务返回的响应 ID、模型字段和白名单 token 用量，按运行累计。

未返回用量时写 null，不假定零消耗；只累计服务已返回的用量，不推算失败请求的计费。不会保存模型内部推理、原始错误正文或密钥。

## 5. 受控真实自纠探针

examples/run_finish_probe.py 使用单独的首轮测试指令，故意要求模型先声明 completed，触发真实校验拒绝，然后观察模型是否根据反馈补齐业务链路。

这项测试诱导了首轮错误，不能用来估计自然错误率。工具调用消息仍由实际模型生成；测试条件与指令全文、指令指纹在 test-conditions.json 和 Trace 中披露，不注入伪造的模型动作。

运行命令（会调用真实 API，请选择新目录）：

```bash
export PYTHONPATH="$PWD/src"
python3 examples/run_finish_probe.py --output runs/finish-probe-new
```

主要证据入口：

- [升级后离线评估](../examples/mock/upgraded-03/report.json)
- [升级后真实评估](../examples/real/deepseek-upgraded-03/report.json)
- [自动测试原始结果](../examples/test-results.txt)

最终验证为 87 个自动测试、14/14 离线场景、14/14 真实场景；[受控真实探针](../examples/real/finish-recovery-probe-03/report.json)也通过。常规真实评估历史保留，重复 Mock 与旧受控探针已清理；新断言与新场景的结果只以本次升级后报告为准。
