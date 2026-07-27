import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.features.llm_analyzer import LLMAnalyzer
from src.storage.sqlite_store import SQLiteStore


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class LLMCacheTests(unittest.TestCase):
    def test_sqlite_store_saves_and_hits_llm_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(str(Path(tmpdir) / "brand3.sqlite3"))
            store.save_llm_cache(
                cache_key="abc",
                prompt_version="v1",
                model="m",
                response_type="json",
                response_json={"ok": True},
            )

            cached = store.get_llm_cache("abc")
            cached_again = store.get_llm_cache("abc")
            store.close()

        self.assertEqual(cached["response_json"], {"ok": True})
        self.assertEqual(cached_again["hit_count"], 1)

    def test_sqlite_store_cache_hit_survives_hit_count_update_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(str(Path(tmpdir) / "brand3.sqlite3"))
            store.save_llm_cache(
                cache_key="abc",
                prompt_version="v1",
                model="m",
                response_type="json",
                response_json={"ok": True},
            )

            with patch.object(
                store,
                "_update_llm_cache_hit_count",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                cached = store.get_llm_cache("abc")
            store.save_llm_cache(
                cache_key="def",
                prompt_version="v1",
                model="m",
                response_type="text",
                response_text="still usable",
            )
            followup = store.get_llm_cache("def")
            store.close()

        self.assertEqual(cached["response_json"], {"ok": True})
        self.assertEqual(cached["hit_count"], 0)
        self.assertEqual(followup["response_text"], "still usable")

    def test_call_json_reuses_persistent_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            first = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            second = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", json.dumps({"score": 88})),
                ) as llm_http:
                    self.assertEqual(first._call_json("system", "user"), {"score": 88})
                    self.assertEqual(second._call_json("system", "user"), {"score": 88})

            self.assertEqual(llm_http.call_count, 1)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(first.cache_writes, 1)

    def test_call_json_cache_separates_temperature_and_schema_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            schema_a = {
                "type": "object",
                "required": ["score"],
                "properties": {"score": {"type": "number"}},
            }
            schema_b = {
                "type": "object",
                "required": ["score", "reason"],
                "properties": {
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
            }
            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    side_effect=[
                        ("ok", json.dumps({"score": 1})),
                        ("ok", json.dumps({"score": 2})),
                        ("ok", json.dumps({"score": 3, "reason": "changed schema"})),
                    ],
                ) as llm_http:
                    first = llm._call_json(
                        "system",
                        "user",
                        json_schema=schema_a,
                        schema_name="score",
                        temperature=0.0,
                    )
                    warmer = llm._call_json(
                        "system",
                        "user",
                        json_schema=schema_a,
                        schema_name="score",
                        temperature=0.1,
                    )
                    changed_schema = llm._call_json(
                        "system",
                        "user",
                        json_schema=schema_b,
                        schema_name="score",
                        temperature=0.1,
                    )

        self.assertEqual(first["score"], 1)
        self.assertEqual(warmer["score"], 2)
        self.assertEqual(changed_schema["reason"], "changed schema")
        self.assertEqual(llm_http.call_count, 3)

    def test_call_json_records_usage_observations_for_cache_and_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            first = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            second = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", json.dumps({"score": 88}), {"total_tokens": 123}),
                ):
                    self.assertEqual(first._call_json("system", "user"), {"score": 88})
                    self.assertEqual(second._call_json("system", "user"), {"score": 88})

        first_summary = first.usage_observation_summary()
        second_summary = second.usage_observation_summary()

        self.assertEqual(first_summary["provider_calls"], 1)
        self.assertEqual(first_summary["cache_misses"], 1)
        self.assertEqual(first_summary["cache_writes"], 1)
        self.assertTrue(first_summary["usage_metadata_available"])
        self.assertEqual(first_summary["observations"][1]["usage_metadata"]["total_tokens"], 123)
        self.assertEqual(second_summary["cache_hits"], 1)
        self.assertEqual(second_summary["observations"][0]["event"], "cache_hit")

    def test_call_json_can_disable_persistent_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            first = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            second = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            first.use_cache = False
            second.use_cache = False
            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    side_effect=[
                        ("ok", json.dumps({"score": 88})),
                        ("ok", json.dumps({"score": 77})),
                    ],
                ) as llm_http:
                    self.assertEqual(first._call_json("system", "user"), {"score": 88})
                    self.assertEqual(second._call_json("system", "user"), {"score": 77})

            self.assertEqual(llm_http.call_count, 2)
            self.assertEqual(first.cache_writes, 0)
            self.assertEqual(second.cache_hits, 0)

    def test_call_text_reuses_persistent_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            first = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            second = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", "cached prose"),
                ) as llm_http:
                    self.assertEqual(first._call("system", "user"), "cached prose")
                    self.assertEqual(second._call("system", "user"), "cached prose")

            self.assertEqual(llm_http.call_count, 1)
            self.assertEqual(second.cache_hits, 1)

    def test_call_text_uses_normalized_chat_completions_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test/", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", "cached prose"),
                ) as llm_http:
                    result = llm._call("system", "user")

        self.assertEqual(result, "cached prose")
        self.assertEqual(llm_http.call_args.kwargs["url"], "https://llm.test/chat/completions")

    def test_call_json_timeout_returns_empty_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("timeout", "llm_call_timeout_after_1s"),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {})
        self.assertEqual(llm.last_failure_reason, "llm_timeout")
        self.assertEqual(llm.call_failures[0]["reason"], "llm_timeout")
        self.assertEqual(llm.call_failures[0]["error_type"], "timeout")
        self.assertEqual(llm.call_failures[0]["model"], "model-a")
        self.assertEqual(llm.call_failures[0]["base_url"], "https://llm.test")

    def test_call_json_empty_response_records_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="secret-key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", ""),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {})
        failure = llm.call_failures[0]
        self.assertEqual(failure["reason"], "llm_error")
        self.assertEqual(failure["error_type"], "empty_response")
        self.assertTrue(failure["response_empty"])
        self.assertFalse(failure["json_parse_error"])
        self.assertEqual(failure["model"], "model-a")
        self.assertEqual(failure["base_url"], "https://llm.test")
        self.assertNotIn("secret-key", json.dumps(failure))

    def test_call_json_http_error_records_provider_http_error(self):
        provider_error = json.dumps({
            "error": {
                "code": "rate_limit_exceeded",
                "message": "Too many requests",
            }
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="secret-key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("http_error", f"HTTP 429: {provider_error}"),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {})
        failure = llm.call_failures[0]
        self.assertEqual(failure["reason"], "provider_http_error")
        self.assertEqual(failure["error_type"], "http_error")
        self.assertEqual(failure["http_status"], 429)
        self.assertEqual(failure["provider_error_code"], "rate_limit_exceeded")
        self.assertEqual(failure["provider_error_message"], "Too many requests")
        self.assertEqual(failure["model"], "model-a")
        self.assertEqual(failure["base_url"], "https://llm.test")
        self.assertNotIn("secret-key", json.dumps(failure))

    def test_call_json_debug_transport_captures_final_url_and_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="secret-key", base_url="https://generativelanguage.googleapis.com/v1beta/openai/", model="model-a")
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "message"],
                "properties": {
                    "status": {"type": "string"},
                    "message": {"type": "string"},
                },
            }

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch.dict("os.environ", {"BRAND3_LLM_DEBUG_TRANSPORT": "1"}, clear=False):
                    with patch(
                        "src.features.llm_analyzer._run_llm_http_call",
                        return_value=("http_error", "HTTP 404: Not Found"),
                    ) as llm_http:
                        result = llm._call_json(
                            "system",
                            "user",
                            json_schema=schema,
                            schema_name="brand3_smoke_test",
                            timeout_seconds=30,
                        )

        self.assertEqual(result, {})
        self.assertEqual(llm.last_failure_reason, "provider_http_error")
        self.assertEqual(llm.last_request_debug["final_url_repr"], "'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'")
        self.assertEqual(llm.last_request_debug["method"], "POST")
        self.assertEqual(llm.last_request_debug["headers"]["Authorization"], "Bearer [redacted]")
        self.assertEqual(llm.last_request_debug["headers"]["Content-Type"], "application/json")
        self.assertEqual(
            llm.last_request_debug["body_top_level_keys"],
            ["max_tokens", "messages", "model", "response_format", "temperature"],
        )
        self.assertEqual(llm_http.call_args.kwargs["url"], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

    def test_call_json_transport_error_records_transport_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="secret-key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("error", "<urlopen error [Errno 8] nodename nor servname provided, or not known>"),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {})
        failure = llm.call_failures[0]
        self.assertEqual(failure["reason"], "transport_error")
        self.assertEqual(failure["error_type"], "transport_error")
        self.assertEqual(llm.last_failure_reason, "transport_error")
        self.assertEqual(failure["model"], "model-a")
        self.assertEqual(failure["base_url"], "https://llm.test")
        self.assertNotIn("secret-key", json.dumps(failure))

    def test_call_json_invalid_json_records_parse_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="secret-key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", "not-json"),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {})
        failure = llm.call_failures[0]
        self.assertEqual(failure["error_type"], "json_parse_error")
        self.assertTrue(failure["json_parse_error"])
        self.assertFalse(failure["response_empty"])
        self.assertEqual(failure["model"], "model-a")
        self.assertEqual(failure["base_url"], "https://llm.test")
        self.assertNotIn("secret-key", json.dumps(failure))

    def test_call_json_schema_validation_error_records_schema_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="secret-key", base_url="https://llm.test", model="model-a")
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["score"],
                "properties": {"score": {"type": "number"}},
            }

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", json.dumps({"wrong": 1})),
                ):
                    result = llm._call_json("system", "user", json_schema=schema, schema_name="score_schema")

        self.assertEqual(result, {})
        failure = llm.call_failures[0]
        self.assertEqual(failure["reason"], "schema_validation_error")
        self.assertEqual(failure["error_type"], "schema_validation_error")
        self.assertEqual(llm.last_failure_reason, "schema_validation_error")
        self.assertEqual(failure["model"], "model-a")
        self.assertEqual(failure["base_url"], "https://llm.test")
        self.assertNotIn("secret-key", json.dumps(failure))

    def test_call_json_extracts_payload_from_provider_prose(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", 'Here is the JSON:\n{"score": 88}\nDone.'),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {"score": 88})
        self.assertIsNone(llm.last_failure_reason)
        self.assertEqual(llm.call_failures, [])

    def test_call_json_success_path_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", json.dumps({"score": 88})),
                ):
                    result = llm._call_json("system", "user")

        self.assertEqual(result, {"score": 88})
        self.assertIsNone(llm.last_failure_reason)
        self.assertEqual(llm.call_failures, [])

    def test_call_json_can_send_openai_compatible_json_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["score"],
                "properties": {"score": {"type": "number"}},
            }

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", json.dumps({"score": 88})),
                ) as llm_http:
                    result = llm._call_json(
                        "system",
                        "user",
                        json_schema=schema,
                        schema_name="score_schema",
                    )

        self.assertEqual(result, {"score": 88})
        body = json.loads(llm_http.call_args.kwargs["payload"].decode("utf-8"))
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["name"], "score_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)

    def test_call_json_gemini_native_sends_response_format_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(
                api_key="key",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                model="gemini-3.5-flash",
            )
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["score"],
                "properties": {"score": {"type": "number"}},
            }

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_gemini_http_call",
                    return_value=("ok", json.dumps({"score": 88})),
                ) as llm_http:
                    result = llm._call_json_gemini_native(
                        "system",
                        "user",
                        json_schema=schema,
                        schema_name="score_schema",
                    )

        self.assertEqual(result, {"score": 88})
        self.assertEqual(
            llm_http.call_args.kwargs["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent",
        )
        body = json.loads(llm_http.call_args.kwargs["payload"].decode("utf-8"))
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "system")
        self.assertEqual(body["generationConfig"]["temperature"], 0.0)
        self.assertEqual(body["generationConfig"]["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")
        self.assertEqual(body["generationConfig"]["responseFormat"]["text"]["schema"], schema)

    def test_call_json_can_override_timeout_per_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    return_value=("ok", json.dumps({"score": 88})),
                ) as llm_http:
                    result = llm._call_json("system", "user", timeout_seconds=90)

        self.assertEqual(result, {"score": 88})
        self.assertEqual(llm_http.call_args.kwargs["timeout_seconds"], 90)

    def test_call_json_schema_mode_falls_back_to_json_object_when_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "brand3.sqlite3")
            llm = LLMAnalyzer(api_key="key", base_url="https://llm.test", model="model-a")
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["score"],
                "properties": {"score": {"type": "number"}},
            }

            with patch("src.features.llm_analyzer.BRAND3_DB_PATH", db_path):
                with patch(
                    "src.features.llm_analyzer._run_llm_http_call",
                    side_effect=[
                        ("error", "HTTP 400: schema mode unsupported"),
                        ("ok", json.dumps({"score": 77})),
                    ],
                ) as llm_http:
                    result = llm._call_json(
                        "system",
                        "user",
                        json_schema=schema,
                        schema_name="score_schema",
                    )

        self.assertEqual(result, {"score": 77})
        first_body = json.loads(llm_http.call_args_list[0].kwargs["payload"].decode("utf-8"))
        second_body = json.loads(llm_http.call_args_list[1].kwargs["payload"].decode("utf-8"))
        self.assertEqual(first_body["response_format"]["type"], "json_schema")
        self.assertEqual(second_body["response_format"], {"type": "json_object"})
        self.assertIsNone(llm.last_failure_reason)


if __name__ == "__main__":
    unittest.main()
