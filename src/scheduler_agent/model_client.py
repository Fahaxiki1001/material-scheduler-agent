"""Chat Completions HTTP 适配器与显式离线流程替身。

使用标准库实现窄接口，减少安装成本；未实现 SDK 的流式、自动重试等功能。
"""
from __future__ import annotations

import json
import os
import copy
import ssl
import sys
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .models import DomainError, Request, slot


def load_dotenv(path: str | Path = ".env") -> None:
    path = Path(path)
    if not path.exists():
        return
    allowed = {"AGENT_API_KEY", "AGENT_MODEL", "AGENT_BASE_URL", "AGENT_TIMEOUT_SECONDS",
               "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "AGENT_CA_BUNDLE"}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in allowed:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # 不执行 shell，也不做变量替换。系统环境变量优先。
        os.environ.setdefault(key, value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DomainError("MODEL_HTTP_REDIRECT", "模型服务返回重定向，请直接配置最终 API 地址")


def create_tls_context(ca_bundle: str | None = None) -> ssl.SSLContext:
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    paths = ssl.get_default_verify_paths()
    # python.org 的 macOS 安装可能没有运行证书安装脚本；使用现有系统 CA，仍验证证书和主机名。
    if (sys.platform == "darwin" and not paths.cafile and not paths.capath
            and not os.getenv("SSL_CERT_FILE") and not os.getenv("SSL_CERT_DIR")
            and Path("/etc/ssl/cert.pem").is_file()):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


class HTTPModelClient:
    mode = "real"

    def __init__(self, *, api_key: str, model: str, base_url: str, timeout: float = 45,
                 ca_bundle: str | None = None):
        if not api_key or not model:
            raise DomainError("MODEL_NOT_CONFIGURED", "真实模式需要配置 AGENT_API_KEY 和 AGENT_MODEL；可先使用 --mode mock")
        parsed = urllib.parse.urlparse(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("API 地址需为 HTTPS（本地服务可用 HTTP），不得包含用户名、密码、查询或片段")
        if not 1 <= timeout <= 120:
            raise ValueError("模型超时需在 1–120 秒之间")
        self.api_key, self.label, self.timeout = api_key, model, timeout
        self.provider = "deepseek" if parsed.hostname == "api.deepseek.com" else "compatible"
        if self.provider == "deepseek" and parsed.path.rstrip("/") not in ("", "/v1"):
            raise ValueError("DeepSeek 适配使用标准接口 https://api.deepseek.com，不支持 Beta 严格模式")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=create_tls_context(ca_bundle)))

    @classmethod
    def from_env(cls):
        base_url = os.getenv("AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        deepseek = urllib.parse.urlparse(base_url).hostname == "api.deepseek.com"
        provider_key = "DEEPSEEK_API_KEY" if deepseek else "OPENAI_API_KEY"
        return cls(api_key=os.getenv("AGENT_API_KEY") or os.getenv(provider_key, ""),
                   model=os.getenv("AGENT_MODEL") or os.getenv("OPENAI_MODEL", ""),
                   base_url=base_url,
                   timeout=float(os.getenv("AGENT_TIMEOUT_SECONDS", "45")), ca_bundle=os.getenv("AGENT_CA_BUNDLE") or None)

    def complete(self, history: list, schemas: list) -> dict:
        payload = {"model": self.label, "messages": history, "tools": schemas,
                   "tool_choice": "required", "parallel_tool_calls": False}
        if self.provider == "deepseek":
            # DeepSeek 的 required 工具选择不能与默认思考模式同时启用。
            # 标准端点不宣称提供 Beta strict 保证，执行前仍使用本地严格参数校验。
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = 0
            payload["max_tokens"] = 2048
            payload.pop("parallel_tool_calls")
            payload["tools"] = copy.deepcopy(schemas)
            for tool in payload["tools"]:
                tool["function"].pop("strict", None)
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.api_key}",
                                                  "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise DomainError("MODEL_RESPONSE_TOO_LARGE", "模型响应超出大小限制")
            data = json.loads(raw)
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            usage = data.get("usage", {})
            metadata = {"provider": self.provider, "usage": {
                key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0}}
            for source, target in (("id", "response_id"), ("model", "response_model")):
                if isinstance(data.get(source), str) and len(data[source]) <= 256:
                    metadata[target] = data[source]
            return {"role": "assistant", "content": None, "tool_calls": message.get("tool_calls", []),
                    "response_metadata": metadata}
        except urllib.error.HTTPError as exc:
            raise DomainError("MODEL_HTTP_ERROR", "模型服务返回 HTTP 错误，请核对服务状态与配置",
                              retryable=exc.code in (408, 429, 500, 502, 503, 504), http_status=exc.code) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise DomainError("MODEL_TLS_ERROR", "模型服务证书校验失败，请检查受信任 CA 配置") from exc
            raise DomainError("MODEL_NETWORK_ERROR", "无法连接模型服务，请检查网络", retryable=True) from exc
        except (TimeoutError, OSError) as exc:
            raise DomainError("MODEL_NETWORK_ERROR", "模型请求超时或网络连接失败", retryable=True) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DomainError("MODEL_RESPONSE_INVALID", "模型服务返回格式无效") from exc


def tool_message(name: str, arguments: dict, reason: str) -> dict:
    if name == "finish":
        arguments = {"suggested_start": None, **arguments}
    return {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function", "function": {"name": name,
            "arguments": json.dumps({**arguments, "reason": reason}, ensure_ascii=False)}}]}


class OfflineModelClient:
    """确定性策略替身，仅证明流程和守卫可运行，不证明 LLM 决策效果。"""
    mode = "mock"
    label = "offline-policy-v1 (NOT A REAL MODEL)"

    def complete(self, history: list, schemas: list) -> dict:
        request = Request.from_dict(json.loads(history[1]["content"]))
        observations = []
        for index, message in enumerate(history):
            if message["role"] == "tool":
                call = history[index - 1]["tool_calls"][0]
                observations.append((call["function"]["name"], json.loads(message["content"])))
        # 结束校验反馈不修改业务事实；离线替身继续根据既有业务观察选择动作。
        observations = [(name, result) for name, result in observations if name != "finish"]
        if observations and not observations[-1][1]["ok"]:
            name, result = observations[-1]
            if name == "read_material" and result["error"]["retryable"]:
                return tool_message("read_material", {"material_id": request.material_id}, "读取暂时失败，重试一次")
            return tool_message("finish", {"status": "failed"}, "工具失败，停止本次处理")
        successful = {name: result["data"] for name, result in observations if result["ok"]}
        if "save_draft" in successful:
            return tool_message("finish", {"status": "completed"}, "已取得真实草稿保存回执")
        if "read_material" not in successful:
            return tool_message("read_material", {"material_id": request.material_id}, "先读取当前素材与已有草稿")
        previous = successful["read_material"].get("existing_draft")
        if previous:
            return tool_message("save_draft", previous["slot"], "相同请求已有草稿，获取幂等回执")
        if "check_rules" not in successful:
            return tool_message("check_rules", {"material_id": request.material_id}, "依据素材元数据核对审核规则")
        audit = successful["check_rules"]
        if audit["audit_status"] == "blocked":
            return tool_message("finish", {"status": "blocked"}, "存在规则阻断风险")
        if audit["missing_fields"]:
            return tool_message("finish", {"status": "needs_input"}, "素材资料不足，需要补充信息")
        if audit["requires_confirmation"]:
            return tool_message("finish", {"status": "needs_confirmation"}, "人工复核需用户确认")
        if "query_slots" not in successful:
            target = request.approved_alternative["start"] if request.approved_alternative else request.requested_start
            return tool_message("query_slots", {"starts": [target]}, "查询用户指定或明确确认的时段")
        queried = {}
        for name, observation in observations:
            if name == "query_slots" and observation["ok"]:
                queried.update({item["start"]: item for item in observation["data"]["slots"]})
        free = [v for v in queried.values() if v["available"] and v["authorization"] != "outside_scope"]
        if free:
            choice = free[0]
            if choice["authorization"] == "needs_confirmation":
                return tool_message("finish", {"status": "needs_confirmation", "suggested_start": choice["start"]}, "找到替代时段，但变更硬性要求需要确认")
            return tool_message("save_draft", {k: choice[k] for k in ("start", "end")}, "审核和时段均满足要求，保存草稿")
        candidates = successful["query_slots"]["candidate_starts"]
        remaining = [start for start in candidates if start not in queried]
        if remaining:
            return tool_message("query_slots", {"starts": remaining}, "原时段冲突，补查其他候选时段")
        return tool_message("finish", {"status": "blocked"}, "已查询全部候选，没有可行时段")
