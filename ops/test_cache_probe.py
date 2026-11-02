import json
import contextlib
import io
import pathlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops.cache_probe import (
    build_request, checked_usage, collect_response, validate_final_result,
    validate_tool_result, fixture_result, make_instructions, is_output_event, session_order, run,
)


class CacheProbeTests(unittest.TestCase):
    def test_complete_probe_keeps_isolated_native_sessions_and_linked_tool_history(self):
        requests = []

        def invoke(base, key, headers, payload):
            requests.append(json.loads(json.dumps({"headers": headers, "payload": payload})))
            if payload["tool_choice"] == "none":
                output = [{"type": "message", "content": [{"type": "output_text", "text":
                    payload["input"][-1]["content"].split(": ", 1)[1]}]}]
            else:
                step = sum(item.get("type") == "function_call_output" for item in payload["input"]) + 1
                output = [{"type": "function_call", "name": "inspect_public_section",
                           "call_id": f"call-{len(requests)}", "arguments": json.dumps({"step": step})}]
            return {"status": 200, "failure": None, "output": output, "terminal": {
                "type": "response.completed", "response": {"usage": {"input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 50}, "output_tokens": 10}}},
                "ttfo_seconds": 0.1, "first_output_event": "response.function_call_arguments.delta",
                "latency_seconds": 0.2}

        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(execute=True, output=str(pathlib.Path(folder) / "receipt.json"),
                manifest=str(pathlib.Path(folder) / "ids.json"), config="unused", run_id="fixture",
                model="synthetic-model", sessions=2, turns=2, context_chars=4096)
            with patch("ops.cache_probe.public_context", return_value="x" * 4096), \
                 patch("ops.cache_probe.load_local_key", return_value=("synthetic-key", 8317)), \
                 patch("ops.cache_probe.invoke", side_effect=invoke), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            self.assertTrue(json.loads(pathlib.Path(args.output).read_text())["complete"])
            self.assertEqual(pathlib.Path(args.manifest).stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(requests), 6)
        self.assertNotEqual(requests[0]["payload"]["instructions"][:150], requests[1]["payload"]["instructions"][:150])
        for request in requests:
            headers, payload = request["headers"], request["payload"]
            self.assertEqual(headers["Session_id"], headers["Thread-Id"])
            self.assertEqual(headers["Session_id"], payload["prompt_cache_key"])
            calls = {item["call_id"] for item in payload["input"] if item.get("type") == "function_call"}
            results = [item for item in payload["input"] if item.get("type") == "function_call_output"]
            self.assertEqual(calls, {item["call_id"] for item in results})
            for item in results:
                self.assertIn("source", json.loads(item["output"]))

    def test_session_schedule_exposes_rotation_with_two_or_three_accounts(self):
        for accounts in (2, 3):
            cursor = 0
            observed = {0: set(), 1: set()}
            for step in range(1, 6):
                for session in session_order(2, step):
                    observed[session].add(cursor % accounts)
                    cursor += 1
            self.assertTrue(all(len(values) > 1 for values in observed.values()))

    def test_request_has_stable_cache_key_and_forced_named_tool(self):
        request = build_request("model-x", "nonce then context", [{"role": "user", "content": "go"}], "cache-id", {"type": "function", "name": "inspect_public_section"})
        self.assertEqual(request["prompt_cache_key"], "cache-id")
        self.assertEqual(request["tool_choice"], {"type": "function", "name": "inspect_public_section"})
        self.assertTrue(request["stream"])
        self.assertFalse(request["store"])

    def test_sse_collects_native_function_call_and_ttfo_includes_tool_args(self):
        lines = [
            b'data: {"type":"response.created"}\n', b'\n',
            b'data: {"type":"response.output_item.added","item":{"type":"function_call"}}\n', b'\n',
            b'data: {"type":"response.function_call_arguments.delta","delta":"{\\"step\\":"}\n', b'\n',
            b'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"inspect_public_section","call_id":"call-1","arguments":"{\\"step\\":2}"}}\n', b'\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"input_tokens_details":{"cached_tokens":4},"output_tokens":3}}}\n', b'\n',
        ]
        output, terminal, first = collect_response(lines)
        self.assertEqual(first, "response.function_call_arguments.delta")
        result = {"status": 200, "failure": None, "output": output, "terminal": terminal}
        call, _ = validate_tool_result(result, 2)
        self.assertEqual(call["call_id"], "call-1")
        self.assertEqual(checked_usage(terminal), (12, 4, 3))

    def test_tool_output_call_id_is_available_for_continuation_linkage(self):
        event_lines = [b'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"opaque-id","name":"inspect_public_section","arguments":"{\\"step\\":1}"}}\n', b'\n']
        output, _, _ = collect_response(event_lines)
        self.assertEqual(output[0]["call_id"], "opaque-id")
        continuation = {"type": "function_call_output", "call_id": output[0]["call_id"], "output": "fixture"}
        self.assertEqual(continuation["call_id"], "opaque-id")

    def test_missing_usage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing usage"):
            checked_usage({"type": "response.completed", "response": {}})

    def test_invalid_cache_counts_are_rejected(self):
        for cached in (11, True):
            with self.subTest(cached=cached), self.assertRaises(ValueError):
                checked_usage({"type": "response.completed", "response": {"usage": {
                    "input_tokens": 10, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": 1,
                }}})

    def test_output_clock_excludes_empty_item_creation(self):
        self.assertFalse(is_output_event("response.output_item.added"))
        self.assertTrue(is_output_event("response.output_text.delta"))
        self.assertTrue(is_output_event("response.function_call_arguments.delta"))

    def test_error_is_not_hidden_by_later_completed_event(self):
        lines = [b'data: {"type":"error"}\n', b'\n',
                 b'data: {"type":"response.completed","response":{}}\n', b'\n']
        _, terminal, _ = collect_response(lines)
        self.assertEqual(terminal["type"], "error")

    def test_fixture_grows_history_with_source_and_unique_early_nonce(self):
        context = "public source line\n" * 200
        body = json.loads(fixture_result(context, 1))
        self.assertEqual(body["source"], context[:2048])
        left, right = make_instructions("session-a", context), make_instructions("session-b", context)
        self.assertNotEqual(left[:100], right[:100])
        self.assertTrue(left.endswith(context))

    def test_failed_event_on_http_200_is_not_success(self):
        output, terminal, _ = collect_response([b'data: {"type":"error","error":{"code":"upstream_failed"}}\n', b'\n'])
        result = {"status": 200, "failure": None, "output": output, "terminal": terminal}
        with self.assertRaisesRegex(ValueError, "SSE error event"):
            validate_tool_result(result, 1)

    def test_wrong_function_arguments_are_rejected(self):
        terminal = {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1}}}
        result = {"status": 200, "failure": None, "terminal": terminal,
                  "output": [{"type": "function_call", "name": "inspect_public_section", "call_id": "c", "arguments": json.dumps({"step": 9})}]}
        with self.assertRaisesRegex(ValueError, "did not match"):
            validate_tool_result(result, 1)

    def test_final_checksum_text_is_checked(self):
        terminal = {"type": "response.completed", "response": {"usage": {"input_tokens": 2, "input_tokens_details": {"cached_tokens": 1}, "output_tokens": 1}}}
        result = {"status": 200, "failure": None, "terminal": terminal,
                  "output": [{"type": "message", "content": [{"type": "output_text", "text": "CHECKSUM:abc"}]}]}
        validate_final_result(result, "CHECKSUM:abc")
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_final_result(result, "CHECKSUM:def")


if __name__ == "__main__":
    unittest.main()
