import io
import copy
import json
import os
from pathlib import Path
import tempfile
import ssl
import unittest
from unittest.mock import patch
import urllib.error

from scheduler_agent.model_client import HTTPModelClient, NoRedirect, create_tls_context, load_dotenv, tool_message
from scheduler_agent.models import DomainError
from scheduler_agent.tools import TOOL_SCHEMAS


class HTTPClientTests(unittest.TestCase):
    def client(self, **changes):
        return HTTPModelClient(**{"api_key": "test-key-only", "model": "test-model",
                                   "base_url": "https://example.test/v1", **changes})

    def test_request_and_tool_response_protocol(self):
        message = tool_message("finish", {"status": "needs_input"}, "需要补充资料")
        response = io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())
        client = self.client()
        with patch.object(client.opener, "open", return_value=response) as opened:
            actual = client.complete([{"role": "user", "content": "synthetic"}], TOOL_SCHEMAS)
        request = opened.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertEqual(payload["tool_choice"], "required")
        self.assertEqual(actual["tool_calls"], message["tool_calls"])
        self.assertNotIn("test-key-only", json.dumps(payload))

    def test_response_metadata_includes_only_safe_selected_fields(self):
        message = tool_message("finish", {"status": "needs_input"}, "资料不足")
        message["reasoning_content"] = "private-reasoning-must-not-be-recorded"
        raw = {"id": "response-test", "model": "served-model", "choices": [{"message": message}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                         "unknown_field": "must-not-be-recorded"}}
        client = self.client()
        with patch.object(client.opener, "open", return_value=io.BytesIO(json.dumps(raw).encode())):
            result = client.complete([], TOOL_SCHEMAS)
        self.assertEqual(result["response_metadata"]["response_model"], "served-model")
        self.assertEqual(result["response_metadata"]["response_id"], "response-test")
        self.assertEqual(result["response_metadata"]["usage"]["total_tokens"], 120)
        self.assertNotIn("must-not-be-recorded", json.dumps(result))

    def test_http_error_body_not_exposed(self):
        client = self.client()
        error = urllib.error.HTTPError(client.endpoint, 401, "private error test-key-only", {}, io.BytesIO(b"secret response"))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(DomainError) as caught:
                client.complete([], TOOL_SCHEMAS)
        self.assertNotIn("test-key-only", str(caught.exception))
        self.assertNotIn("secret response", str(caught.exception))
        self.assertEqual(caught.exception.code, "MODEL_HTTP_ERROR")
        self.assertEqual(caught.exception.details, {"http_status": 401})

    def test_tls_error_is_distinguished_from_http_error(self):
        client = self.client()
        error = urllib.error.URLError(ssl.SSLCertVerificationError("certificate detail"))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(DomainError) as caught:
                client.complete([], TOOL_SCHEMAS)
        self.assertEqual(caught.exception.code, "MODEL_TLS_ERROR")

    def test_transient_http_error_is_marked_retryable(self):
        client = self.client()
        error = urllib.error.HTTPError(client.endpoint, 503, "private error", {}, io.BytesIO(b"private body"))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(DomainError) as caught:
                client.complete([], TOOL_SCHEMAS)
        self.assertTrue(caught.exception.retryable)

    def test_deepseek_flash_uses_standard_non_thinking_tool_calls(self):
        original = copy.deepcopy(TOOL_SCHEMAS)
        message = tool_message("finish", {"status": "needs_input"}, "需要补充资料")
        response = io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())
        client = self.client(model="deepseek-flash", base_url="https://api.deepseek.com")
        with patch.object(client.opener, "open", return_value=response) as opened:
            client.complete([], TOOL_SCHEMAS)
        request = opened.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["tool_choice"], "required")
        self.assertNotIn("parallel_tool_calls", payload)
        self.assertTrue(all("strict" not in t["function"] for t in payload["tools"]))
        self.assertEqual(TOOL_SCHEMAS, original)

    def test_deepseek_does_not_fall_back_to_openai_credentials(self):
        with patch.dict(os.environ, {"AGENT_BASE_URL": "https://api.deepseek.com", "AGENT_MODEL": "deepseek-flash",
                                    "OPENAI_API_KEY": "must-not-send-to-deepseek"}, clear=True):
            with self.assertRaises(DomainError):
                HTTPModelClient.from_env()

    def test_deepseek_specific_key_is_supported(self):
        with patch.dict(os.environ, {"AGENT_BASE_URL": "https://api.deepseek.com", "AGENT_MODEL": "deepseek-flash",
                                    "DEEPSEEK_API_KEY": "test-deepseek-key-only"}, clear=True):
            client = HTTPModelClient.from_env()
            self.assertEqual(client.provider, "deepseek")
            self.assertEqual(client.api_key, "test-deepseek-key-only")

    def test_deepseek_beta_is_not_mislabelled_as_standard_mode(self):
        with self.assertRaises(ValueError):
            self.client(base_url="https://api.deepseek.com/beta")

    def test_redirect_cannot_forward_bearer_key(self):
        with self.assertRaises(DomainError):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.test")

    def test_missing_credentials_fail_explicitly(self):
        with self.assertRaises(DomainError):
            self.client(api_key="")

    def test_unsafe_urls_are_rejected(self):
        for base_url in ("http://example.test/v1", "https://user:password@example.test/v1", "https://example.test/v1?key=abc"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                self.client(base_url=base_url)

    def test_tls_verification_remains_enabled(self):
        context = create_tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_macos_missing_python_ca_uses_system_ca(self):
        with patch("scheduler_agent.model_client.sys.platform", "darwin"), \
             patch("scheduler_agent.model_client.ssl.get_default_verify_paths") as paths, \
             patch("scheduler_agent.model_client.ssl.create_default_context") as create, \
             patch("scheduler_agent.model_client.Path.is_file", return_value=True), \
             patch.dict(os.environ, {}, clear=True):
            paths.return_value.cafile = paths.return_value.capath = None
            create_tls_context()
        create.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_explicit_ca_configuration_is_honored(self):
        with patch("scheduler_agent.model_client.ssl.create_default_context") as create:
            create_tls_context("/custom/trusted-ca.pem")
        create.assert_called_once_with(cafile="/custom/trusted-ca.pem")

    def test_dotenv_does_not_execute_shell_or_override_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            marker = Path(folder) / "must-not-exist"
            path.write_text(f'AGENT_MODEL="file-model"\nAGENT_API_KEY=$(touch {marker})\nIGNORED=value\n')
            with patch.dict(os.environ, {"AGENT_MODEL": "env-model"}, clear=True):
                load_dotenv(path)
                self.assertEqual(os.environ["AGENT_MODEL"], "env-model")
                self.assertTrue(os.environ["AGENT_API_KEY"].startswith("$(touch"))
                self.assertNotIn("IGNORED", os.environ)
            self.assertFalse(marker.exists())
