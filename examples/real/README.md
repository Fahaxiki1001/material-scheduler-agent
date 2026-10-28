# 真实模型运行记录

已使用 DeepSeek 官方 `deepseek-flash` 完成初始与升级后的真实验收：

- `deepseek-run-01/report.json`：10/12，保留失败。
- `deepseek-run-02/report.json`：改造前 12/12。
- `deepseek-upgraded-01/report.json`：改造后首次 14/14。
- `deepseek-upgraded-02/report.json`：中间复测 12/14，保留失败与评估修正依据。
- `deepseek-upgraded-03/report.json`：最终 **14/14**，评审入口。
- `finish-recovery-probe-03/report.json`：最终代码版本的受控真实自纠探针通过，测试条件单独披露。

每个场景包含输入、实际输出、JSONL Trace、数据副本及数据库快照。详细解释见 `docs/03-evaluation.md`。

在项目根目录配置 `.env`，然后运行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m scheduler_agent evaluate --mode real --output examples/real/deepseek-run-03
```

保留实际生成的输入、输出、Trace、数据快照与报告，无论成功或失败。完成后更新 `docs/03-evaluation.md` 中的真实评估结论。
