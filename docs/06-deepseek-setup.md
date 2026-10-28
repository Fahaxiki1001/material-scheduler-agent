# DeepSeek Flash 配置

核对日期：2026-10-28。模型与接口依据 DeepSeek 官方文档，不根据名称猜测。

## 当前配置

```dotenv
AGENT_API_KEY=
AGENT_MODEL=deepseek-flash
AGENT_BASE_URL=https://api.deepseek.com
AGENT_TIMEOUT_SECONDS=45
```

本地 `.env` 已由用户配置密钥，此处仅展示空白模板，不打印密钥。也支持 `DEEPSEEK_API_KEY`，但不会自动拿 `OPENAI_API_KEY` 发送到 DeepSeek。`.env` 已被 Git 忽略。

## 适配方式

- 请求地址为 `https://api.deepseek.com/chat/completions`。
- 使用 `deepseek-flash`；官方当前列出的底层版本为 DeepSeek-V4.1-Flash-1017。别名的后端版本可能变化，以服务实际返回和官方文档为准。
- 显式发送 `thinking: {"type": "disabled"}`，因为官方说明思考模式不支持 `tool_choice=required`。
- 使用标准接口，不启用要求 `/beta` 的 strict 模式；发送时移除 strict 标记，不修改本地原始 schema。
- 不发送该接口文档未声明的 `parallel_tool_calls`。每回合单工具调用由提示和本地 Runner 强制检查，多个调用不会被执行。
- 业务工具参数与保存授权仍在本地严格校验；去掉服务端 strict 标记不会移除业务守卫。
- 每次模型响应最多 2048 tokens，保留原有调用次数上限。
- 非思考模式下设置 temperature=0，减少无关的采样变化；这不保证输出完全确定。

## 验证状态

87 个自动测试通过，包括 DeepSeek 请求参数、凭证隔离、证书校验、错误分类及结束自纠测试。最新真实验收 14/14，通过记录见 `examples/real/deepseek-upgraded-03/report.json`；受控真实探针也通过，历史失败记录保留。

可用新的数据库与输出目录复现：

```bash
export PYTHONPATH="$PWD/src"
python3 -m scheduler_agent run --input examples/inputs/normal.json --mode real --db runs/deepseek-smoke.db
python3 -m scheduler_agent evaluate --mode real --output examples/real/deepseek-run-03
```

如果 macOS 的 Python 缺少默认 CA 路径且未显式配置 SSL_CERT_FILE/SSL_CERT_DIR，客户端会使用已有系统 `/etc/ssl/cert.pem`。也可用 `AGENT_CA_BUNDLE=/path/to/trusted-ca.pem` 指定受信任 CA。证书与主机名校验始终开启，不能通过关闭 TLS 验证解决。

## 官方来源

- [模型与地址](https://api-docs.deepseek.com/quick_start/pricing)
- [Chat Completions 参数，含 thinking 与 tool_choice 限制](https://api-docs.deepseek.com/api/create-chat-completion)
- [工具调用与 strict 模式要求](https://api-docs.deepseek.com/guides/tool_calls)
