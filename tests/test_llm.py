"""Tests for llm.py against the fake LLM server: requests, replies, errors, retries."""

import copy
import email.utils
import json
import os
import socket
import ssl
import threading
import time
import unittest
import urllib.error
from unittest import mock

import support

import fake_llm

import common
import llm
import prompts

# Image bytes: only their JPEG signature matters to llm.py (it never decodes them).
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

SYSTEM = "You catalogue photos."
USER_TEXT = "Describe this image."

# A schema with every kind of bound, nested objects, a union, and a property
# whose name is a schema keyword (it must survive sanitizing).
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "tags": {"type": "array", "maxItems": 15, "minItems": 0,
                 "items": {"type": "string", "minLength": 1, "maxLength": 60}},
        "aesthetic": {"type": "integer", "minimum": 1, "maximum": 5},
        "ratio": {"type": "number", "exclusiveMinimum": 0, "multipleOf": 0.5},
        "objects": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string", "maxLength": 60},
                           "count": {"type": "integer", "minimum": 1, "maximum": 99}},
            "required": ["name", "count"]}},
        "mood": {"anyOf": [{"type": "string", "enum": ["calm", "neutral"], "maxLength": 9},
                           {"type": "null"}]},
        "minimum": {"type": "string", "minLength": 1},
    },
    "required": ["title", "tags", "aesthetic", "ratio", "objects", "mood", "minimum"],
}

REPLY_DATA = {"title": "A cat on a sofa", "tags": ["cat", "sofa"], "aesthetic": 4,
              "ratio": 1.5, "objects": [{"name": "cat", "count": 1}], "mood": "calm",
              "minimum": "x"}
REPLY = json.dumps(REPLY_DATA)

KEY_VARIABLES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                 "OPENROUTER_API_KEY", "AZURE_OPENAI_API_KEY")
BOUND_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "multipleOf", "minLength",
                  "maxLength", "minItems", "maxItems")


def schema_keywords(node, found=None):
    """Every keyword used anywhere in a schema (property names excluded)."""
    found = set() if found is None else found
    if isinstance(node, list):
        for item in node:
            schema_keywords(item, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key == "properties":
                for sub in value.values():
                    schema_keywords(sub, found)
            elif isinstance(value, (dict, list)) and key not in ("enum", "required"):
                schema_keywords(value, found)
    return found


class LlmTestCase(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM(reply=REPLY).start()
        self.addCleanup(self.server.stop)
        # Keys set on the machine running the tests must not leak in.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in KEY_VARIABLES + ("LOG_ERRORS",):
            os.environ.pop(name, None)
        llm._no_stream_options.clear()
        self.addCleanup(llm._no_stream_options.clear)

    def endpoint(self, kind="openai", **settings):
        base = self.server.base_url if kind == "openai" else self.server.root_url
        endpoint = {"name": "test", "kind": kind, "base_url": base, "model": "fake-model"}
        endpoint.update(settings)
        return endpoint

    def describe(self, endpoint, schema=SCHEMA, **kwargs):
        return llm.describe(endpoint, SYSTEM, USER_TEXT, JPEG, schema, **kwargs)

    def last(self):
        return self.server.describe_requests()[-1]

    def raises(self, endpoint, kind, **kwargs):
        with self.assertRaises(llm.ProviderError) as caught:
            self.describe(endpoint, **kwargs)
        self.assertEqual(caught.exception.kind, kind, str(caught.exception))
        return caught.exception


class OpenAIRequestTest(LlmTestCase):

    def test_local_server_request(self):
        reply = self.describe(self.endpoint())
        self.assertEqual(reply["text"], REPLY)
        self.assertEqual((reply["finish"], reply["model"]), ("ok", "fake-model"))
        self.assertEqual(reply["usage"], {"input_tokens": 10, "output_tokens": 20})
        self.assertGreaterEqual(reply["seconds"], 0)
        request = self.last()
        self.assertTrue(request["path"].endswith("/v1/chat/completions"))
        # The image comes before the text, and the system prompt is separate.
        self.assertEqual(request["parts"], ["image_url", "text"])
        self.assertEqual((request["system"], request["prompt"]), (SYSTEM, USER_TEXT))
        self.assertEqual((request["mime"], request["image"]), ("image/jpeg", JPEG))
        self.assertEqual(request["detail"], "high")
        payload = request["payload"]
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["max_tokens"], 4000)
        self.assertNotIn("max_completion_tokens", payload)
        # A local llama.cpp-like server: thinking off, and the full schema
        # (its grammar uses every bound).
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        response_format = payload["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(response_format["json_schema"]["schema"], SCHEMA)
        self.assertNotIn("authorization", request["headers"])
        self.assertNotIn("provider", payload)

    def test_cloud_presets(self):
        cases = {
            # preset: (token field, sends chat_template_kwargs)
            "openai": ("max_completion_tokens", False),
            "azure": ("max_completion_tokens", False),
            "gemini": ("max_tokens", False),
            "openrouter": ("max_tokens", False),
            "custom": ("max_tokens", False),
            "llamacpp": ("max_tokens", True),
            "lmstudio": ("max_tokens", False),
        }
        for preset, (tokens, template) in cases.items():
            with self.subTest(preset=preset):
                # Not a real key: a made-up value used only by this test.
                self.describe(self.endpoint(preset=preset, api_key="test-key-0001"))
                request = self.last()
                payload = request["payload"]
                self.assertEqual(payload[tokens], 4000)
                other = ("max_tokens" if tokens == "max_completion_tokens"
                         else "max_completion_tokens")
                self.assertNotIn(other, payload)
                self.assertEqual("chat_template_kwargs" in payload, template)
                self.assertEqual(request["headers"]["authorization"], "Bearer test-key-0001")
                keywords = schema_keywords(payload["response_format"]["json_schema"]["schema"])
                if preset in common.LOCAL_PRESETS:
                    self.assertIn("maxLength", keywords)
                else:
                    # Cloud services reject string lengths but keep the others.
                    self.assertFalse(keywords & {"minLength", "maxLength"}, keywords)
                    self.assertTrue({"minimum", "maximum", "maxItems"} <= keywords, keywords)
                self.assertEqual("provider" in payload, preset == "openrouter")
                if preset == "openrouter":
                    # Only providers that honour the schema and keep no data.
                    self.assertEqual(payload["provider"], {"require_parameters": True,
                                                           "data_collection": "deny"})

    def test_extra_settings_are_merged(self):
        endpoint = self.endpoint(extra={
            "temperature": 0.1, "chat_template_kwargs": {"reasoning": "off"},
            "max_tokens": None, "max_completion_tokens": 300})
        self.describe(endpoint, request_extra={"seed": 7, "repeat_penalty": 1.1})
        payload = self.last()["payload"]
        self.assertEqual(payload["temperature"], 0.1)
        self.assertEqual(payload["chat_template_kwargs"],
                         {"enable_thinking": False, "reasoning": "off"})
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["max_completion_tokens"], 300)
        self.assertEqual((payload["seed"], payload["repeat_penalty"]), (7, 1.1))
        # null removes a default field.
        self.describe(self.endpoint(extra={"chat_template_kwargs": None}))
        self.assertNotIn("chat_template_kwargs", self.last()["payload"])
        # The endpoint's settings are not changed by a request.
        self.assertEqual(endpoint["extra"]["chat_template_kwargs"], {"reasoning": "off"})

    def test_mime_follows_the_bytes_and_no_schema_asks_for_json(self):
        llm.describe(self.endpoint(), "", USER_TEXT, PNG, None)
        request = self.last()
        self.assertEqual(request["mime"], "image/png")
        self.assertEqual(request["payload"]["response_format"], {"type": "json_object"})
        # No system prompt: no system message.
        self.assertEqual([m["role"] for m in request["payload"]["messages"]], ["user"])

    def test_stream_options_rejected_once_then_left_out(self):
        self.server.reject_stream_options = True
        reply = self.describe(self.endpoint())
        self.assertEqual(reply["text"], REPLY)
        self.assertEqual(reply["usage"], {"input_tokens": None, "output_tokens": None})
        first, second = self.server.describe_requests()
        self.assertIn("stream_options", first["payload"])
        self.assertNotIn("stream_options", second["payload"])
        # Remembered for this endpoint: one request from now on.
        self.describe(self.endpoint())
        self.assertEqual(len(self.server.describe_requests()), 3)
        self.assertNotIn("stream_options", self.last()["payload"])

    def test_concurrent_requests(self):
        self.server.reply = fake_llm.Reply(REPLY, delay=0.3)
        errors = []

        def run():
            try:
                self.describe(self.endpoint())
            except Exception as error:      # reported below
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(self.server.max_in_flight, 4)


class ClaudeRequestTest(LlmTestCase):

    def test_request_shape(self):
        # Not a real key: a made-up value used only by this test.
        endpoint = self.endpoint("claude", model="claude-haiku-5-5", api_key="test-key-0002")
        reply = self.describe(endpoint)
        self.assertEqual(reply["text"], REPLY)
        self.assertEqual(reply["model"], "claude-haiku-5-5")
        request = self.last()
        self.assertEqual(request["path"], "/v1/messages")
        headers = request["headers"]
        self.assertEqual(headers["x-api-key"], "test-key-0002")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", headers)
        self.assertEqual(request["parts"], ["image", "text"])
        self.assertEqual((request["system"], request["prompt"]), (SYSTEM, USER_TEXT))
        self.assertEqual(request["mime"], "image/jpeg")
        payload = request["payload"]
        self.assertEqual(payload["max_tokens"], 4000)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        for name in ("temperature", "top_p", "top_k", "stream"):
            self.assertNotIn(name, payload)
        output_format = payload["output_config"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        schema = output_format["schema"]
        self.assertFalse(schema_keywords(schema) & set(BOUND_KEYWORDS))
        self.assertIn("minimum", schema["properties"])
        self.assertEqual(schema["required"], SCHEMA["required"])
        self.assertNotIn("effort", payload["output_config"])

    def test_thinking_per_model(self):
        cases = {
            "claude-haiku-5-5": ({"type": "disabled"}, None, 4000),
            "claude-haiku-5-5-20260101": ({"type": "disabled"}, None, 4000),
            "claude-sonnet-5-5": ({"type": "between_tools"}, None, 4000),
            "claude-opus-5-5": (None, "low", 8000),
            "claude-fable-5-1": (None, "low", 8000),
            "claude-older-model": (None, None, 4000),
        }
        for model, (thinking, effort, max_tokens) in cases.items():
            with self.subTest(model=model):
                self.describe(self.endpoint("claude", model=model))
                payload = self.last()["payload"]
                self.assertEqual(payload.get("thinking"), thinking)
                self.assertEqual(payload["output_config"].get("effort"), effort)
                self.assertEqual(payload["max_tokens"], max_tokens)
                self.assertNotIn("budget_tokens", json.dumps(payload))

    def test_extra_merges_into_nested_settings(self):
        endpoint = self.endpoint("claude", model="claude-opus-5-5",
                                 extra={"output_config": {"effort": "medium"}},
                                 max_output_tokens=12000)
        self.describe(endpoint)
        payload = self.last()["payload"]
        self.assertEqual(payload["output_config"]["effort"], "medium")
        self.assertIn("format", payload["output_config"])
        self.assertEqual(payload["max_tokens"], 12000)

    def test_base_url_ending_in_v1(self):
        self.describe(self.endpoint("claude", base_url=self.server.root_url + "/v1/"))
        self.assertEqual(self.last()["path"], "/v1/messages")

    def test_base_path_of_a_gateway_is_kept(self):
        # Only Ollama's base URL loses a trailing /api.
        self.describe(self.endpoint("claude", base_url=self.server.root_url + "/api"))
        self.assertEqual(self.last()["path"], "/api/v1/messages")
        self.describe(self.endpoint("claude", base_url=self.server.root_url + "/api/v1"))
        self.assertEqual(self.last()["path"], "/api/v1/messages")
        self.describe(self.endpoint("ollama", base_url=self.server.root_url + "/api"))
        self.assertEqual(self.last()["path"], "/api/chat")


class OllamaRequestTest(LlmTestCase):

    def test_request_shape(self):
        reply = self.describe(self.endpoint("ollama"))
        self.assertEqual(reply["text"], REPLY)
        self.assertEqual(reply["usage"], {"input_tokens": 10, "output_tokens": 20})
        request = self.last()
        self.assertEqual(request["path"], "/api/chat")
        payload = request["payload"]
        self.assertFalse(payload["stream"])
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["format"], SCHEMA)
        self.assertEqual(payload["options"], {"num_predict": 4000})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": SYSTEM})
        self.assertEqual((request["prompt"], request["image"]), (USER_TEXT, JPEG))

    def test_cut_off_reply(self):
        self.server.queue(fake_llm.Reply("{\"title\": \"A ca", finish="length"))
        reply = self.describe(self.endpoint("ollama"))
        self.assertEqual(reply["finish"], "length")


class ReplyTest(LlmTestCase):

    def test_openai_finish_reasons(self):
        cases = [
            (fake_llm.Reply(REPLY, finish="length"), "length", ""),
            (fake_llm.Reply("", refusal="I can't help with that."), "refusal",
             "I can't help with that."),
            (fake_llm.Reply("", finish="filtered"), "filtered", ""),
            (fake_llm.Reply(REPLY, thinking="Let me look..."), "ok", ""),
        ]
        for response, finish, detail in cases:
            with self.subTest(finish=finish):
                self.server.queue(response)
                reply = self.describe(self.endpoint())
                self.assertEqual((reply["finish"], reply["detail"]), (finish, detail))
                self.assertNotIn("Let me look", reply["text"])

    def test_openai_reply_details(self):
        self.server.queue(fake_llm.Reply(REPLY, usage=None, model="served-model"))
        reply = self.describe(self.endpoint())
        self.assertEqual(reply["usage"], {"input_tokens": None, "output_tokens": None})
        self.assertEqual(reply["model"], "served-model")

    def test_openai_reply_that_is_not_streamed(self):
        self.server.queue(fake_llm.Reply(REPLY, finish="length", usage=(5, 6)))
        reply = self.describe(self.endpoint(),
                              request_extra={"stream": False, "stream_options": None})
        self.assertEqual((reply["text"], reply["finish"]), (REPLY, "length"))
        self.assertEqual(reply["usage"], {"input_tokens": 5, "output_tokens": 6})

    def test_broken_streams_are_retryable(self):
        self.server.queue(fake_llm.Reply(REPLY, done=False))
        self.assertIn("ended early", str(self.raises(self.endpoint(), "retryable")))
        self.server.queue(fake_llm.Reply(REPLY, stream_error={"code": 502,
                                                              "message": "upstream failed"}))
        error = self.raises(self.endpoint(), "retryable")
        self.assertEqual(error.status, 502)
        self.server.queue(fake_llm.Reply(REPLY, stream_error={"message": "boom"}))
        self.assertIn("boom", str(self.raises(self.endpoint(), "retryable")))
        # A server error reported inside the stream follows the usual rules.
        self.server.queue(fake_llm.Reply(REPLY, stream_error={"code": 402,
                                                              "message": "no credits"}))
        self.raises(self.endpoint(), "fatal")

    def test_errors_inside_the_stream(self):
        start = 'data: {"choices": [{"delta": {"content": "{"}}]}\n\n'
        cases = [
            # llama.cpp's llama-server: an "error:" field.
            (start + 'error: {"code": 400, "message": "failed to process image", '
                     '"type": "invalid_request_error"}\n\n', "image", 400,
             "failed to process image"),
            # An "event: error" whose data is the error.
            ('event: error\ndata: {"error": {"code": 500, "message": "slot crashed"}}\n\n',
             "retryable", 500, "slot crashed"),
            (start + "error: something broke\n\n", "retryable", None, "something broke"),
            # Other events are ignored.
            ('event: ping\ndata: {"choices": [{"delta": {"content": "{}"}, '
             '"finish_reason": "stop"}]}\n\ndata: [DONE]\n\n', None, None, None),
        ]
        for text, kind, status, message in cases:
            with self.subTest(text=text[:40]):
                self.server.queue(fake_llm.RawStream(text))
                if kind is None:
                    self.assertEqual(self.describe(self.endpoint())["text"], "{}")
                    continue
                error = self.raises(self.endpoint(), kind)
                self.assertEqual(error.status, status)
                self.assertIn(message, str(error))
        # The image error is not asked again.
        self.server.queue(fake_llm.RawStream(cases[0][0]))
        with self.assertRaises(llm.ProviderError):
            llm.describe_with_retries(self.endpoint(), SYSTEM, USER_TEXT, JPEG, SCHEMA,
                                      sleep=lambda seconds: None)
        self.assertEqual(len(self.server.describe_requests()), len(cases) + 1)

    def test_claude_replies(self):
        endpoint = self.endpoint("claude")
        # Thinking blocks come first; the answer is the first text block.
        self.server.queue(fake_llm.Reply(REPLY, thinking="The image shows..."))
        reply = self.describe(endpoint)
        self.assertEqual((reply["text"], reply["finish"]), (REPLY, "ok"))
        self.server.queue(fake_llm.Reply(finish="refusal", refusal="Not allowed."))
        reply = self.describe(endpoint)
        self.assertEqual((reply["text"], reply["finish"], reply["reason"], reply["detail"]),
                         ("", "refusal", "refusal", "Not allowed."))
        self.server.queue(fake_llm.Reply(REPLY[:20], finish="length"))
        reply = self.describe(endpoint)
        self.assertEqual((reply["finish"], reply["reason"]), ("length", "max_tokens"))

    def test_claude_usage_counts_cached_input(self):
        body = {"type": "message", "model": "m", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": REPLY}],
                "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                          "cache_read_input_tokens": 1000, "output_tokens": 7}}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(body).encode("utf-8")
        with mock.patch.object(llm, "_urlopen", return_value=response):
            reply = self.describe(self.endpoint("claude"))
        self.assertEqual(reply["usage"], {"input_tokens": 1105, "output_tokens": 7})


class ErrorTest(LlmTestCase):

    def test_classify_http(self):
        def body(message, **error):
            error["message"] = message
            return json.dumps({"error": error})

        cases = [
            (429, body("Rate limit reached"), {"Retry-After": "3"}, "retryable", 3.0),
            (429, body("Rate limit reached"), None, "retryable", None),
            (429, body("x", type="insufficient_quota"), None, "fatal", None),
            (429, json.dumps({"type": "error", "error": {
                "type": "rate_limit_error", "message": "limit",
                "details": {"error_code": "enforced_spend_limit_reached"}}}), None,
             "fatal", None),
            # Ordinary rate limits mention quotas and billing too: only
            # explicit codes are fatal.
            (429, body("You exceeded your current quota, please check your plan and billing "
                       "details."), None, "retryable", None),
            (429, body("You exceeded your current quota", details=[
                {"retryDelay": "27s"}]), None, "retryable", 27.0),
            (429, body("Rate limit reached for m in organization org-x on tokens per min "
                       "(TPM): Limit 30000. Please try again in 1.898s. You can increase "
                       "your rate limit by adding a payment method at "
                       "https://platform.openai.com/account/billing.",
                       type="tokens", code="rate_limit_exceeded"), None, "retryable", 1.898),
            (429, body("Please try again in 7m12s."), None, "retryable", 432.0),
            (429, body("Please retry in 20ms"), None, "retryable", 0.02),
            # A daily quota sets the endpoint aside (other endpoints go on).
            (429, body("Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"), None,
             "endpoint", None),
            (429, body("Rate limit reached for m on requests per day (RPD): Limit 200, Used "
                       "200. Please try again in 8.64s.", type="requests"),
             {"Retry-After": "9"}, "endpoint", 9.0),
            (529, body("Overloaded"), None, "retryable", None),
            (500, "", None, "retryable", None),
            (502, "<html>bad gateway</html>", None, "retryable", None),
            (503, body("Loading model"), None, "retryable", None),
            (504, "", None, "retryable", None),
            (520, "", None, "retryable", None),
            (522, "<html>connection timed out</html>", None, "retryable", None),
            (524, "api.openai.com | 524: A timeout occurred", None, "retryable", None),
            (507, "", None, "retryable", None),
            (530, "", None, "retryable", None),
            (499, "", None, "retryable", None),
            (505, "", None, "endpoint", None),
            (301, "", None, "endpoint", None),
            (408, "", None, "retryable", None),
            (409, "", None, "retryable", None),
            (425, "", None, "retryable", None),
            (401, body("invalid x-api-key"), None, "fatal", None),
            # Gemini reports a bad key as 400.
            (400, json.dumps([{"error": {"code": 400, "message": "API key not valid. Please "
                                         "pass a valid API key.", "status": "INVALID_ARGUMENT",
                                         "details": [{"reason": "API_KEY_INVALID"}]}}]),
             None, "fatal", None),
            # A content policy (moderation) blocked this image: the image's problem.
            (400, body("The response was filtered due to the prompt triggering Azure "
                       "OpenAI's content management policy.", code="content_filter"), None,
             "image", None),
            (400, json.dumps({"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "Output blocked by content filtering policy"}}), None, "image", None),
            (403, body('Your chosen model requires moderation and your input was flagged for '
                       '"sexual"', code=403, metadata={"flagged_input": "..."}), None,
             "image", None),
            (403, body("permission denied"), None, "fatal", None),
            (402, body("Insufficient credits"), None, "fatal", None),
            (400, body("Your credit balance is too low to access the API."), None, "fatal",
             None),
            (404, body("The model `nope` does not exist"), None, "endpoint", None),
            (400, body("Invalid schema for response_format 'reply'"), None, "endpoint", None),
            (400, body("output_config.format.schema: 'maximum' is not supported"), None,
             "endpoint", None),
            (422, body("Unsupported parameter: 'max_tokens'"), None, "endpoint", None),
            (400, body("max_tokens is too large: 100000"), None, "endpoint", None),
            (400, body("image exceeds 5 MB maximum"), None, "image", None),
            (400, body("Could not process image"), None, "image", None),
            (400, body("The image dimensions exceed the limit of 8000 pixels"), None, "image",
             None),
            (413, body("request_too_large"), None, "image", None),
            (400, body("image input is not supported - you may need to provide the mmproj"),
             None, "endpoint", None),
            (400, body("Invalid content type. image_url is only supported by certain models."),
             None, "endpoint", None),
            (400, body("something else"), None, "endpoint", None),
            (405, "", None, "endpoint", None),
            (501, "", None, "endpoint", None),
        ]
        for status, text, headers, kind, retry_after in cases:
            with self.subTest(status=status, body=text):
                self.assertEqual(llm.classify_http(status, text, headers), (kind, retry_after))

    def test_retry_after_forms(self):
        self.assertEqual(llm.retry_after_seconds({"retry-after": "12"}), 12.0)
        self.assertEqual(llm.retry_after_seconds({"Retry-After": "1.5"}), 1.5)
        self.assertEqual(llm.retry_after_seconds({"retry-after-ms": "2500",
                                                  "retry-after": "9"}), 2.5)
        when = email.utils.formatdate(time.time() + 30, usegmt=True)
        self.assertAlmostEqual(llm.retry_after_seconds({"Retry-After": when}), 30, delta=2)
        past = email.utils.formatdate(time.time() - 30, usegmt=True)
        self.assertEqual(llm.retry_after_seconds({"Retry-After": past}), 0.0)
        gemini = json.dumps([{"error": {"details": [{"retryDelay": "27.5s"}]}}])
        self.assertEqual(llm.retry_after_seconds({}, gemini), 27.5)
        self.assertIsNone(llm.retry_after_seconds({"Retry-After": "soon"}))
        self.assertEqual(llm.retry_after_seconds({}, "Please try again in 1h2m3.5s."), 3723.5)
        self.assertEqual(llm.retry_after_seconds({}, "Try again in 250ms"), 0.25)
        self.assertIsNone(llm.retry_after_seconds({}, "try again in 5 minutes"))
        self.assertIsNone(llm.retry_after_seconds(None, ""))

    def test_errors_from_the_server(self):
        cases = [
            (fake_llm.rate_limited(retry_after=7), "retryable", 429, 7.0),
            (fake_llm.rate_limited(retry_after_ms=1500), "retryable", 429, 1.5),
            (fake_llm.gemini_rate_limited("27s"), "retryable", 429, 27.0),
            (fake_llm.overloaded(), "retryable", 529, None),
            (fake_llm.HttpError(500), "retryable", 500, None),
            (fake_llm.unauthorized(), "fatal", 401, None),
            (fake_llm.no_credit(), "fatal", 402, None),
            (fake_llm.spend_limit(), "fatal", 429, None),
            (fake_llm.insufficient_quota(), "fatal", 429, None),
            (fake_llm.schema_error(), "endpoint", 400, None),
            (fake_llm.image_error(), "image", 400, None),
            (fake_llm.no_vision_error(), "endpoint", 400, None),
            (fake_llm.HttpError(413), "image", 413, None),
            (fake_llm.HttpError(404), "endpoint", 404, None),
        ]
        for kind_name in ("openai", "claude", "ollama"):
            for response, kind, status, retry_after in cases:
                with self.subTest(endpoint=kind_name, status=status, kind=kind):
                    self.server.queue(response)
                    error = self.raises(self.endpoint(kind_name), kind)
                    self.assertEqual((error.status, error.retry_after), (status, retry_after))
                    self.assertIn("HTTP %d" % status, str(error))

    def test_connection_problems(self):
        self.server.queue(fake_llm.Disconnect())
        error = self.raises(self.endpoint(), "retryable")
        self.assertIsNone(error.status)
        cases = [
            (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), "retryable"),
            (urllib.error.URLError(socket.timeout("timed out")), "retryable"),
            (socket.timeout("timed out"), "retryable"),
            (ConnectionResetError(), "retryable"),
            (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "endpoint"),
            (urllib.error.URLError("unknown url type: ftp"), "endpoint"),
            # Passing DNS and TLS trouble is worth another try...
            (urllib.error.URLError(socket.gaierror(11002, "getaddrinfo failed")), "retryable"),
            (urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name "
                                                       "resolution")), "retryable"),
            (urllib.error.URLError(ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] "
                                                      "EOF occurred")), "retryable"),
            (ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed"), "retryable"),
            (ssl.SSLSyscallError(5, "Some I/O error occurred"), "retryable"),
            # ...a certificate that does not check out, or https:// to a plain
            # HTTP server, is not.
            (urllib.error.URLError(ssl.SSLCertVerificationError(
                1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")), "endpoint"),
            (urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version "
                                                   "number")), "endpoint"),
        ]
        for problem, kind in cases:
            with self.subTest(problem=repr(problem)):
                with mock.patch.object(llm, "_urlopen", side_effect=problem):
                    error = self.raises(self.endpoint(), kind)
                self.assertIs(error.__cause__, problem)
        with mock.patch.object(llm, "_urlopen",
                               side_effect=urllib.error.URLError(socket.timeout())):
            self.assertIn("no answer within 5 seconds",
                          str(self.raises(self.endpoint(timeout=5), "retryable")))

    def test_redirects_are_never_followed(self):
        # A redirect would carry the key headers (and the picture) elsewhere.
        collector = fake_llm.FakeLLM().start()
        self.addCleanup(collector.stop)
        # Not a real key: a made-up value used only by this test.
        key = "test-key-redirect-0001"
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.server.redirect = fake_llm.Redirect(collector.base_url + "/elsewhere",
                                                         status)
                for kind in ("openai", "claude", "ollama"):
                    error = self.raises(self.endpoint(kind, api_key=key), "endpoint")
                    self.assertEqual(error.status, status)
                    self.assertIn("redirects the request to %s/elsewhere"
                                  % collector.base_url, str(error))
                    self.assertIn("redirects requests elsewhere",
                                  llm.explain_error(error, self.endpoint(kind)))
                info = llm.probe(self.endpoint(preset="llamacpp", api_key=key))
                self.assertIsNone(info["model"])
                self.assertEqual(llm.list_models(self.endpoint(api_key=key)), [])
                self.assertEqual(llm.list_models(self.endpoint("claude", api_key=key)), [])
                llm.wait_until_ready(self.endpoint(preset="vllm", api_key=key), 0.2,
                                     sleep=lambda seconds: None)
                self.assertEqual(collector.requests, [])
        self.assertTrue(self.server.requests)

    def test_keys_are_masked_before_error_texts_are_cut(self):
        # Not a real key: a made-up value used only by this test.
        key = "SENTINELKEY9f8e7d6c5b4a3210fedcba"
        endpoint = self.endpoint(api_key=key)
        for start in range(llm.ERROR_TEXT_LIMIT - 12, llm.ERROR_TEXT_LIMIT + 2):
            message = "q" * (start - 1) + " " + key + " more text"
            with self.subTest(start=start):
                texts = [
                    str(llm.error_from_http(400, json.dumps({"error": {"message": message}}),
                                            None, [key])),
                    str(llm._stream_error({"message": message}, [key])),
                    str(llm._stream_error({"code": 400, "message": message}, [key])),
                ]
                self.server.queue(fake_llm.HttpError(400, {"error": {"message": message}}))
                error = self.raises(endpoint, "endpoint")
                texts += [str(error), llm.explain_error(error, endpoint)]
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = json.dumps({"error": message}).encode("utf-8")
                with mock.patch.object(llm, "_urlopen", return_value=response):
                    texts.append(str(self.raises(self.endpoint("ollama", api_key=key),
                                                 "endpoint")))
                for text in texts:
                    self.assertNotIn(key[:3], text)

    def test_missing_base_url(self):
        error = self.raises({"name": "x", "preset": "azure", "model": "m"}, "endpoint")
        self.assertIn("base URL", str(error))

    def test_keys_never_appear_in_errors_or_logs(self):
        # None of these are real keys: they are made-up values used only by this test.
        keys = ("plain-secret-value-123", "sk-proj-abcdefghijklmnopqrstuvwxyz0123")
        for key in keys:
            with self.subTest(key=key[:6]):
                endpoint = self.endpoint(api_key=key)
                self.server.queue(fake_llm.unauthorized(echo=key))
                with mock.patch.dict(os.environ, {"LOG_ERRORS": "1"}):
                    error = self.raises(endpoint, "fatal")
                texts = [str(error), repr(error), error.message, " ".join(map(str, error.args)),
                         llm.explain_error(error, endpoint)]
                for name in os.listdir(common.logs_dir()):
                    with open(os.path.join(common.logs_dir(), name), encoding="utf-8") as handle:
                        texts.append(handle.read())
                self.server.queue(fake_llm.unauthorized(echo=key))
                texts.append(json.dumps(llm.test_endpoint(endpoint, JPEG)))
                for text in texts:
                    self.assertNotIn(key, text)
                self.assertIn(common.REDACTED, str(error))
                # The key was really sent (and only in the header).
                request = self.last()
                self.assertEqual(request["headers"]["authorization"], "Bearer " + key)
                self.assertNotIn(key, json.dumps(request["payload"]))

    def test_explain_error(self):
        endpoint = {"name": "cloud", "preset": "openai", "model": "m",
                    "base_url": self.server.base_url}
        self.server.queue(fake_llm.unauthorized())
        error = self.raises(endpoint, "fatal")
        self.assertEqual(llm.explain_error(error, endpoint),
                         "The OpenAI endpoint 'cloud' rejected the API key (401). "
                         "No API key is set.")
        # Not a real key: a made-up value used only by this test.
        os.environ["OPENAI_API_KEY"] = "test-key-0006"
        endpoint["api_key_env"] = "OPENAI_API_KEY"
        self.server.queue(fake_llm.unauthorized())
        self.assertTrue(llm.explain_error(self.raises(endpoint, "fatal"), endpoint).endswith(
            "Check the key (env OPENAI_API_KEY)."))
        # Gemini's 400 for a bad key gets the same advice.
        self.server.queue(fake_llm.HttpError(400, [{"error": {
            "code": 400, "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}]))
        text = llm.explain_error(self.raises(endpoint, "fatal"), endpoint)
        self.assertIn("rejected the API key (400). Check the key (env OPENAI_API_KEY).", text)
        self.server.queue(fake_llm.HttpError(429, {"error": {
            "message": "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"}}))
        self.assertIn("reached a usage limit (429)",
                      llm.explain_error(self.raises(endpoint, "endpoint"), endpoint))
        self.server.queue(fake_llm.HttpError(400, {"error": {
            "code": "content_filter", "message": "filtered by the content management policy"}}))
        self.assertIn("blocked the image under its content policy (400)",
                      llm.explain_error(self.raises(endpoint, "image"), endpoint))
        self.server.queue(fake_llm.no_credit())
        text = llm.explain_error(self.raises(endpoint, "fatal"), endpoint)
        self.assertIn("no credit", text)
        self.server.queue(fake_llm.HttpError(404))
        text = llm.explain_error(self.raises(endpoint, "endpoint"))
        self.assertTrue(text.startswith("The endpoint does not know the model"), text)
        self.server.queue(fake_llm.image_error())
        self.assertIn("rejected the image",
                      llm.explain_error(self.raises(endpoint, "image"), endpoint))


class RetryTest(LlmTestCase):

    def setUp(self):
        super().setUp()
        # The longest possible random backoff, so delays are predictable.
        patcher = mock.patch.object(llm.random, "uniform", side_effect=lambda low, high: high)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = []

    def run_retries(self, endpoint=None, **kwargs):
        return llm.describe_with_retries(endpoint or self.endpoint(), SYSTEM, USER_TEXT, JPEG,
                                         SCHEMA, sleep=self.sleeps.append, **kwargs)

    def test_backoff_honours_retry_after(self):
        self.server.queue(fake_llm.rate_limited(retry_after=7), fake_llm.overloaded())
        seen = []
        reply = self.run_retries(on_retry=lambda error, delay: seen.append((error.status, delay)))
        self.assertEqual(reply["text"], REPLY)
        self.assertEqual(reply["retries"], 2)
        self.assertEqual(self.sleeps, [7.0, 2])
        self.assertEqual(seen, [(429, 7.0), (529, 2)])
        self.assertEqual(len(self.server.describe_requests()), 3)

    def test_gives_up_after_max_retries(self):
        self.server.reply = fake_llm.HttpError(500)
        with self.assertRaises(llm.ProviderError) as caught:
            self.run_retries(max_retries=3)
        self.assertEqual(caught.exception.kind, "retryable")
        self.assertEqual(self.sleeps, [1, 2, 4])
        self.assertEqual(len(self.server.describe_requests()), 4)

    def test_backoff_is_capped(self):
        self.server.reply = fake_llm.HttpError(503)
        with self.assertRaises(llm.ProviderError):
            self.run_retries(max_retries=8)
        self.assertEqual(self.sleeps, [1, 2, 4, 8, 16, 32, 60, 60])

    def test_other_failures_are_not_retried(self):
        for response, kind in ((fake_llm.unauthorized(), "fatal"),
                               (fake_llm.schema_error(), "endpoint"),
                               (fake_llm.image_error(), "image")):
            with self.subTest(kind=kind):
                before = len(self.server.describe_requests())
                self.server.queue(response)
                with self.assertRaises(llm.ProviderError) as caught:
                    self.run_retries()
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(len(self.server.describe_requests()), before + 1)
        self.assertEqual(self.sleeps, [])

    def test_a_very_long_retry_after_is_not_waited_for(self):
        self.server.queue(fake_llm.rate_limited(retry_after=llm.MAX_RETRY_AFTER + 1))
        with self.assertRaises(llm.ProviderError) as caught:
            self.run_retries()
        self.assertEqual(caught.exception.retry_after, llm.MAX_RETRY_AFTER + 1)
        self.assertEqual(self.sleeps, [])

    def test_timeouts_are_retried_less(self):
        with mock.patch.object(llm, "_urlopen",
                               side_effect=urllib.error.URLError(socket.timeout("timed out"))):
            for limit, calls in ((0, 1), (2, 3), (None, 5)):
                with self.subTest(limit=limit):
                    self.sleeps.clear()
                    with self.assertRaises(llm.ProviderError) as caught:
                        self.run_retries(max_retries=4, max_timeout_retries=limit)
                    self.assertTrue(caught.exception.timeout)
                    self.assertEqual(caught.exception.kind, "retryable")
                    self.assertEqual(len(self.sleeps), calls - 1)
        # Other retryable failures still get every retry.
        self.server.queue(fake_llm.HttpError(503), fake_llm.HttpError(503))
        self.assertEqual(self.run_retries(max_timeout_retries=0)["retries"], 2)

    def test_stop_request_ends_the_wait(self):
        self.server.reply = fake_llm.rate_limited(retry_after=30)
        with self.assertRaises(llm.ProviderError):
            self.run_retries(should_stop=lambda: len(self.sleeps) >= 3)
        # Waits are cut in slices so a stop request is noticed quickly.
        self.assertEqual(self.sleeps, [0.5, 0.5, 0.5])
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.sleeps.clear()
        with self.assertRaises(llm.ProviderError):
            self.run_retries(should_stop=lambda: True)
        self.assertEqual(self.sleeps, [])


class SanitizeTest(unittest.TestCase):

    def test_per_provider(self):
        original = copy.deepcopy(SCHEMA)
        claude = llm.sanitize_schema(SCHEMA, {"kind": "claude"})
        self.assertFalse(schema_keywords(claude) & set(BOUND_KEYWORDS))
        self.assertIn("minimum", claude["properties"])
        self.assertEqual(claude["properties"]["mood"]["anyOf"][0]["enum"], ["calm", "neutral"])
        self.assertEqual(claude["properties"]["objects"]["items"]["required"],
                         ["name", "count"])
        self.assertFalse(claude["additionalProperties"])
        for preset in ("openai", "azure", "gemini", "openrouter", "custom"):
            cloud = llm.sanitize_schema(SCHEMA, {"preset": preset})
            keywords = schema_keywords(cloud)
            self.assertFalse(keywords & {"minLength", "maxLength"}, preset)
            self.assertTrue({"minimum", "maximum", "maxItems", "multipleOf"} <= keywords, preset)
        for endpoint in ({"preset": "llamacpp"}, {"base_url": "http://127.0.0.1:8080/v1"},
                         {"kind": "ollama"}, {"preset": "lmstudio"}):
            self.assertEqual(llm.sanitize_schema(SCHEMA, endpoint), SCHEMA, endpoint)
        self.assertEqual(SCHEMA, original)
        self.assertIsNone(llm.sanitize_schema(None, {"kind": "claude"}))


class ProbeTest(LlmTestCase):

    def test_llamacpp_props(self):
        self.server.props_data = {"model_path": "C:\\path\\to\\model.gguf", "total_slots": 4,
                                  "modalities": {"vision": True}}
        # Not a real key: a made-up value used only by this test.
        endpoint = self.endpoint(api_key="test-key-0003")
        info = llm.probe(endpoint)
        self.assertEqual(info, {"model": "model.gguf", "slots": 4, "vision": True,
                                "server": "llama.cpp", "reachable": True, "loading": False})
        [request] = self.server.other_requests()
        self.assertEqual(request["path"], "/props")
        self.assertEqual(request["headers"]["authorization"], "Bearer test-key-0003")
        self.assertEqual(common.endpoint_concurrency(endpoint, info["slots"]), 4)
        self.assertEqual(llm.probe_model_name(endpoint), "model.gguf")

    def test_server_without_props_or_still_loading(self):
        self.server.props = False
        info = llm.probe(self.endpoint())
        self.assertEqual((info["model"], info["reachable"], info["loading"]), (None, True, False))
        self.assertEqual(llm.probe_model_name(self.endpoint()), "fake-model")
        self.server.props = True
        self.server.props_status = 503
        info = llm.probe(self.endpoint())
        self.assertEqual((info["model"], info["loading"]), (None, True))

    def test_unreachable_server_never_raises(self):
        with mock.patch.object(llm, "_urlopen",
                               side_effect=urllib.error.URLError(ConnectionRefusedError())):
            for kind in ("openai", "ollama"):
                info = llm.probe(self.endpoint(kind))
                self.assertIs(info["reachable"], False, kind)
                self.assertIsNone(info["model"])
            info = llm.probe(self.endpoint(preset="lmstudio"))
            self.assertIs(info["reachable"], False)
            with mock.patch.object(llm, "PROPS_RETRY_DELAY", 0):
                self.assertEqual(llm.probe_model_name(self.endpoint()), "fake-model")
            self.assertEqual(llm.list_models(self.endpoint()), [])
        self.assertIsNone(llm.probe("not an endpoint")["reachable"])

    def test_cloud_services_are_not_contacted(self):
        for preset in ("openai", "anthropic", "gemini"):
            info = llm.probe(self.endpoint(preset=preset))
            self.assertEqual((info["server"], info["reachable"]), (preset, None))
        self.assertEqual(self.server.requests, [])

    def test_ollama(self):
        info = llm.probe(self.endpoint("ollama"))
        self.assertEqual(info["server"], "ollama 0.0.1-fake")
        self.assertEqual((info["model"], info["vision"], info["reachable"]),
                         ("fake-model", True, True))
        self.server.capabilities = ["completion"]
        self.assertIs(llm.probe(self.endpoint("ollama"))["vision"], False)

    def test_other_local_servers(self):
        info = llm.probe(self.endpoint(preset="vllm"))
        self.assertEqual((info["model"], info["reachable"], info["server"]),
                         (None, True, "vllm"))
        self.server.models = ["only-model"]
        self.assertEqual(llm.probe(self.endpoint(preset="vllm"))["model"], "only-model")


class WaitUntilReadyTest(LlmTestCase):

    def test_waits_while_the_model_loads(self):
        self.server.health_script.extend([503, 503])
        sleeps = []
        result = llm.wait_until_ready(self.endpoint(), 60, interval=0.25, sleep=sleeps.append)
        self.assertTrue(result["ready"])
        self.assertEqual((result["status"], result["loading"], result["error"]),
                         (200, False, ""))
        self.assertEqual(sleeps, [0.25, 0.25])
        self.assertEqual([r["path"] for r in self.server.other_requests()], ["/health"] * 3)

    def test_gives_up_after_the_timeout(self):
        self.server.health_status = 503
        result = llm.wait_until_ready(self.endpoint(), 0.3, interval=0.05)
        self.assertFalse(result["ready"])
        self.assertTrue(result["loading"])
        self.assertIn("loading", result["error"])
        self.assertGreaterEqual(result["waited"], 0.3)

    def test_stop_and_unreachable(self):
        self.server.health_status = 503
        result = llm.wait_until_ready(self.endpoint(), 60, should_stop=lambda: True,
                                      sleep=lambda seconds: None)
        self.assertEqual((result["ready"], result["error"]), (False, "stopped"))
        with mock.patch.object(llm, "_urlopen",
                               side_effect=urllib.error.URLError(ConnectionRefusedError())):
            result = llm.wait_until_ready(self.endpoint(), 0.1, interval=0.02)
        self.assertFalse(result["ready"])
        self.assertIn("cannot connect", result["error"])

    def test_other_servers(self):
        self.assertTrue(llm.wait_until_ready(self.endpoint(preset="openai"), 5)["ready"])
        self.assertEqual(self.server.requests, [])
        self.assertTrue(llm.wait_until_ready(self.endpoint("ollama"), 5)["ready"])
        self.assertEqual(self.server.other_requests()[-1]["path"], "/api/version")


class TestEndpointTest(LlmTestCase):

    def test_success(self):
        # Not a real key: a made-up value used only by this test.
        endpoint = self.endpoint(api_key="test-key-0004", price_in=1.0, price_out=2.0)
        result = llm.test_endpoint(endpoint, JPEG)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["error_kind"], result["error"]), ("", ""))
        self.assertEqual(result["parsed"], REPLY_DATA)
        # A llama.cpp-like server: the model it loaded, not the name it echoes.
        self.assertEqual(result["served_model"], "model.gguf")
        self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 20})
        self.assertAlmostEqual(result["est_cost_per_image"], (10 * 1.0 + 20 * 2.0) / 1e6)
        self.assertEqual(result["key_source"], "config")
        self.assertIsInstance(result["latency_ms"], int)
        # The real prompt and schema were sent.
        system, user_text, schema = llm._test_prompt(None, None, None)
        request = self.last()
        self.assertEqual((request["system"], request["prompt"]), (system, user_text))
        self.assertEqual(request["payload"]["response_format"]["json_schema"]["schema"],
                         llm.sanitize_schema(schema, endpoint))
        self.assertEqual((system, user_text), (prompts.SYSTEM_PROMPT, "Describe this image."))
        self.assertEqual(schema, prompts.schema("core"))

    def test_served_model_from_the_probe(self):
        self.server.queue(fake_llm.Reply(REPLY, model=""))
        self.assertEqual(llm.test_endpoint(self.endpoint(), JPEG)["served_model"], "model.gguf")
        self.server.queue(fake_llm.Reply(REPLY, model=""))
        result = llm.test_endpoint(self.endpoint(preset="vllm"), JPEG)
        self.assertEqual(result["served_model"], "fake-model")
        self.assertIsNone(result["est_cost_per_image"])
        # Without /props, the name in the reply.
        self.server.props = False
        self.assertEqual(llm.test_endpoint(self.endpoint(), JPEG)["served_model"], "fake-model")

    def test_failures(self):
        cases = [
            (fake_llm.unauthorized(), "fatal"),
            (fake_llm.schema_error(), "endpoint"),
            (fake_llm.Reply("Sorry, no."), "content"),
            (fake_llm.Reply(REPLY[:30], finish="length"), "content"),
            (fake_llm.Reply("", refusal="No."), "content"),
            (fake_llm.Reply("[1, 2]"), "content"),
        ]
        for response, kind in cases:
            with self.subTest(kind=kind):
                self.server.queue(response)
                result = llm.test_endpoint(self.endpoint(), JPEG)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_kind"], kind)
                self.assertTrue(result["error"])
                self.assertIsNone(result["parsed"])

    def test_custom_parser(self):
        def parse(text):
            raise ValueError("description is missing")
        result = llm.test_endpoint(self.endpoint(), JPEG, parse=parse)
        self.assertEqual(result["error_kind"], "content")
        self.assertIn("description is missing", result["error"])
        result = llm.test_endpoint(self.endpoint(), JPEG, system="S", user_text="U",
                                   schema={"type": "object"}, parse=json.loads)
        self.assertTrue(result["ok"])
        self.assertEqual((self.last()["system"], self.last()["prompt"]), ("S", "U"))


class ListModelsTest(LlmTestCase):

    def test_each_kind(self):
        self.assertEqual(llm.list_models(self.endpoint()), ["fake-model", "other-model"])
        self.assertEqual(self.server.requests[-1]["path"], "/v1/models")
        # Not a real key: a made-up value used only by this test.
        claude = self.endpoint("claude", api_key="test-key-0005")
        self.assertEqual(llm.list_models(claude), ["fake-model", "other-model"])
        request = self.server.requests[-1]
        self.assertTrue(request["path"].startswith("/v1/models?"))
        self.assertEqual(request["headers"]["x-api-key"], "test-key-0005")
        self.assertEqual(llm.list_models(self.endpoint("ollama")), ["fake-model", "other-model"])
        self.assertEqual(self.server.requests[-1]["path"], "/api/tags")


class PresetTest(unittest.TestCase):

    def test_presets(self):
        self.assertIs(llm.PRESETS, common.ENDPOINT_PRESETS)
        for name, preset in llm.PRESETS.items():
            with self.subTest(preset=name):
                self.assertTrue({"kind", "base_url", "key_env", "concurrency", "notes",
                                 "label"} <= set(preset))
                self.assertIn(preset["kind"], common.ENDPOINT_KINDS)
                if name in common.CLOUD_PRESETS:
                    self.assertTrue(preset["key_env"])
                    self.assertEqual(preset["concurrency"], 4)
                    self.assertTrue(preset["base_url"] == "" or
                                    preset["base_url"].startswith("https://"))
                else:
                    self.assertEqual(preset["key_env"], "")
                if preset["base_url"] and name not in common.CLOUD_PRESETS:
                    self.assertTrue(common.is_local_endpoint({"preset": name}), name)


if __name__ == "__main__":
    unittest.main()
