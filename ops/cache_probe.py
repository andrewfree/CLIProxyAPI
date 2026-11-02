#!/usr/bin/env python3
"""Opt-in native Responses tool-loop cache probe. No calls occur without --execute."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "sdk/cliproxy/auth/selector.go",
    "sdk/cliproxy/auth/conductor_selection.go",
    "sdk/cliproxy/session/info.go",
    "sdk/cliproxy/service_config.go",
    "internal/runtime/executor/codex_executor_request.go",
    "sdk/api/handlers/openai/openai_responses_websocket.go",
    "sdk/cliproxy/auth/conductor_refresh.go",
    "sdk/cliproxy/auth/conductor_cooldown.go",
    "internal/runtime/executor/claude_executor_request.go",
)
TOOL_NAME = "inspect_public_section"
REQUEST_TIMEOUT_SECONDS = 180  # Probe-only bound; does not alter proxy timeout behavior.


def public_context(root: pathlib.Path, chars: int) -> str:
    """Build a stable, bounded fixture from an explicit public-source allowlist."""
    parts = []
    for name in SOURCE_FILES:
        path = root / name
        text = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"--- {name} ---\n{text}")
    context = "\n".join(parts)
    if len(context) < chars:
        raise ValueError("requested context exceeds the public fixture")
    return context[:chars]


def make_instructions(run_nonce: str, context: str) -> str:
    # Run-unique material intentionally precedes the common context prefix.
    return (
        f"Synthetic cache probe run nonce: {run_nonce}. Treat all source below as public, "
        "read-only coding context. Follow the requested tool protocol exactly.\n\n"
        f"SHARED PUBLIC CONTEXT\n{context}"
    )


def tool_schema() -> dict:
    return {
        "type": "function", "name": TOOL_NAME,
        "description": "Read one numbered section from the fixed public source fixture.",
        "parameters": {
            "type": "object", "properties": {"step": {"type": "integer"}},
            "required": ["step"], "additionalProperties": False,
        },
        "strict": True,
    }


def build_request(model: str, instructions: str, input_items: list[dict], cache_key: str, tool_choice) -> dict:
    return {
        "model": model, "instructions": instructions, "input": input_items,
        "tools": [tool_schema()], "tool_choice": tool_choice, "stream": True, "store": False,
        "prompt_cache_key": cache_key, "reasoning": {"effort": "low"},
        "parallel_tool_calls": False,
    }


def session_order(count, step):
    order = list(range(count))
    return order if step % 2 else order[::-1]


def parse_sse(lines):
    """Yield decoded JSON data events; ignore comments and malformed/non-data lines."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        if not line:
            if data:
                joined = "\n".join(data)
                data = []
                try:
                    yield json.loads(joined)
                except json.JSONDecodeError:
                    continue
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        try:
            yield json.loads("\n".join(data))
        except json.JSONDecodeError:
            return


def collect_response(lines):
    """Collect output items and terminal response without retaining text in records."""
    output, terminal, first_output_type = [], None, None
    for event in parse_sse(lines):
        if not isinstance(event, dict):
            raise ValueError("invalid event shape")
        kind = event.get("type", "")
        if first_output_type is None and is_output_event(kind):
            first_output_type = kind
        if kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                output.append(item)
        if kind in ("response.failed", "response.incomplete", "error"):
            terminal = event
        elif kind == "response.completed" and (terminal is None or terminal.get("type") == "response.completed"):
            terminal = event
    return output, terminal, first_output_type


def is_output_event(kind):
    return kind in ("response.output_text.delta", "response.function_call_arguments.delta")


def checked_usage(terminal: dict) -> tuple[int, int, int]:
    if terminal.get("type") != "response.completed":
        raise ValueError("response did not complete")
    response = terminal.get("response")
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("missing usage")
    details = usage.get("input_tokens_details")
    values = (usage.get("input_tokens"), details.get("cached_tokens") if isinstance(details, dict) else None,
              usage.get("output_tokens"))
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("incomplete usage")
    if values[1] > values[0]:
        raise ValueError("cached tokens exceed input tokens")
    return values


def fixture_result(context: str, step: int) -> str:
    chunks = [context[i:i + 2048] for i in range(0, len(context), 2048)] or [""]
    if not 1 <= step <= len(chunks):
        raise ValueError("tool step is outside fixture")
    body = chunks[step - 1]
    return json.dumps({"step": step, "sha256": hashlib.sha256(body.encode()).hexdigest(), "source": body}, sort_keys=True)


def save_progress(path: pathlib.Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as destination:
        destination.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def load_local_key(config_path: pathlib.Path) -> tuple[str, int]:
    """Read only port and first configured API key; never render config or key."""
    try:
        try:
            import yaml
        except ImportError:
            raw = subprocess.check_output(
                ["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", str(config_path)],
                text=True, stderr=subprocess.PIPE,
            )
            config = json.loads(raw)
        else:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        keys = config.get("access", {}).get("api-keys", config.get("api-keys"))
        port = config.get("server", {}).get("port", config.get("port"))
        if not isinstance(keys, list) or not keys or not isinstance(keys[0], str) or not keys[0]:
            raise ValueError("missing API key")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        return keys[0], port
    except Exception:
        raise ValueError("cannot read valid local API configuration") from None


def invoke(base_url: str, api_key: str, headers: dict, payload: dict):
    request = urllib.request.Request(
        base_url + "/v1/responses", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json", **headers},
    )
    start = time.monotonic()
    first_at, status, failure = None, None, None
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            lines = []
            for raw in response:
                line = raw.decode("utf-8", "replace")
                lines.append(line)
                if line.startswith("data:"):
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        raise ValueError("invalid event shape")
                    if first_at is None and is_output_event(event.get("type", "")):
                        first_at = time.monotonic() - start
            output, terminal, first_type = collect_response(lines)
    except urllib.error.HTTPError as exc:
        status, failure = exc.code, "HTTPError"
        exc.close()
        output, terminal, first_type = [], None, None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        failure = type(exc).__name__
        output, terminal, first_type = [], None, None
    return {
        "status": status, "output": output, "terminal": terminal, "failure": failure,
        "ttfo_seconds": first_at, "first_output_event": first_type,
        "latency_seconds": time.monotonic() - start,
    }


def validate_tool_result(result: dict, expected_step: int) -> tuple[dict, dict]:
    if result["status"] != 200 or result["failure"]:
        raise ValueError("request failed")
    if result["terminal"] is None:
        raise ValueError("missing terminal event")
    if result["terminal"].get("type") == "error":
        raise ValueError("SSE error event on HTTP response")
    if result["terminal"].get("type") != "response.completed":
        raise ValueError("failed or incomplete terminal event")
    checked_usage(result["terminal"])
    calls = [item for item in result["output"] if item.get("type") == "function_call" and item.get("name") == TOOL_NAME]
    if len(calls) != 1 or sum(item.get("type") == "function_call" for item in result["output"]) != 1:
        raise ValueError("expected exactly one tool call")
    if not isinstance(calls[0].get("arguments"), str):
        raise ValueError("invalid tool arguments")
    try:
        args = json.loads(calls[0]["arguments"])
    except (KeyError, json.JSONDecodeError):
        raise ValueError("invalid tool arguments") from None
    if not isinstance(args, dict) or type(args.get("step")) is not int or args != {"step": expected_step} or not calls[0].get("call_id"):
        raise ValueError("tool arguments did not match expected step")
    return calls[0], result["terminal"]["response"].get("usage", {})


def validate_final_result(result: dict, expected: str) -> dict:
    if result["status"] != 200 or result["failure"]:
        raise ValueError("final request failed")
    if result["terminal"] is None or result["terminal"].get("type") != "response.completed":
        raise ValueError("missing or failed final event")
    checked_usage(result["terminal"])
    fragments = []
    for item in result["output"]:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
            raise ValueError("invalid message content")
        for part in content:
            if part.get("type") == "output_text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("invalid output text")
                fragments.append(part["text"])
    text = "".join(fragments)
    if text.strip() != expected:
        raise ValueError("final output checksum did not match")
    return result["terminal"]["response"]["usage"]


def response_record(index: int, step, result: dict, nonce_hash: str, error):
    terminal = result["terminal"]
    response = terminal.get("response") if terminal else None
    usage = response.get("usage") if isinstance(response, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {"session_index": index, "step": step, "status": result["status"],
        "terminal": (terminal or {}).get("type"), "model": result["model"], "run_nonce_sha256": nonce_hash,
        "input_tokens": usage.get("input_tokens"), "cached_tokens": details.get("cached_tokens"),
        "output_tokens": usage.get("output_tokens"), "ttfo_seconds": result["ttfo_seconds"],
        "first_output_event": result["first_output_event"], "latency_seconds": result["latency_seconds"],
        "failure_type": result["failure"], "validation_error": error}


def run(args) -> int:
    if not args.execute:
        print("No requests sent. Add --execute to enable the probe.")
        return 0
    output_path = pathlib.Path(args.output)
    if output_path.exists() or output_path.with_name(output_path.name + ".partial").exists():
        raise ValueError("output already exists; choose a fresh path")
    if args.manifest:
        manifest = pathlib.Path(args.manifest)
        if manifest.resolve() in (output_path.resolve(), output_path.with_name(output_path.name + ".partial").resolve()):
            raise ValueError("manifest and output paths must be distinct")
        if manifest.exists():
            raise ValueError("manifest already exists; choose a fresh path")
        manifest.parent.mkdir(parents=True, exist_ok=True)
    context = public_context(ROOT, args.context_chars)
    if args.turns > (len(context) + 2047) // 2048:
        raise ValueError("turns exceeds the number of public fixture sections")
    api_key, port = load_local_key(pathlib.Path(args.config))
    run_nonce = args.run_id + ":" + uuid.uuid4().hex
    nonce_hash = hashlib.sha256(run_nonce.encode()).hexdigest()[:16]
    sessions = []
    for index in range(args.sessions):
        session_id = str(uuid.uuid4())
        sessions.append({"session_id": session_id, "thread_id": session_id, "cache_key": session_id,
                         "instructions": make_instructions(run_nonce + ":session:" + str(index), context)})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    if args.manifest:
        identities = [{k: ids[k] for k in ("session_id", "thread_id", "cache_key")} for ids in sessions]
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as destination:
            destination.write(json.dumps({"run_nonce_sha256": nonce_hash, "sessions": identities}, indent=2) + "\n")
    doc = {"run_id": args.run_id, "run_nonce_sha256": nonce_hash, "model": args.model,
           "sessions": args.sessions, "tool_turns": args.turns, "context_chars": len(context),
           "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS, "records": [],
           "context_sha256": hashlib.sha256(context.encode()).hexdigest(), "started_unix": time.time(),
           "instruction_hashes": [hashlib.sha256(ids["instructions"].encode()).hexdigest() for ids in sessions],
           "limitations": ["Synthetic public-source workload; not representative production traffic.",
                           "Cached-token usage is a measurement, not proof of causal savings."]}
    save_progress(output_path, doc)
    base = f"http://127.0.0.1:{port}"
    failed = False
    # Reversing each turn avoids a fixed request order aliasing the account cycle.
    for step in range(1, args.turns + 1):
        for index in session_order(len(sessions), step):
            ids = sessions[index]
            history = [{"role": "user", "content": f"Inspect section {step} using {TOOL_NAME}. Return its step number."}]
            if step == 1:
                history.insert(0, {"role": "user", "content": "Use the shared read-only public source context in instructions."})
            if "history" not in ids:
                ids["history"] = history
            else:
                ids["history"].append({"role": "user", "content": f"Inspect section {step} using {TOOL_NAME}. Return its step number."})
            headers = {"Session_id": ids["session_id"], "Thread-Id": ids["thread_id"],
                       "User-Agent": "cliproxy-cache-probe/1"}
            result = invoke(base, api_key, headers, build_request(args.model, ids["instructions"], ids["history"], ids["cache_key"], {"type": "function", "name": TOOL_NAME}))
            result["model"] = args.model
            error = None
            try:
                call, _ = validate_tool_result(result, step)
                tool_output = fixture_result(context, step)
                ids["history"].extend(result["output"])
                ids["history"].append({"type": "function_call_output", "call_id": call["call_id"], "output": tool_output})
            except ValueError as exc:
                error, failed = str(exc), True
            doc["records"].append(response_record(index, step, result, nonce_hash, error))
            save_progress(output_path, doc)
            print(json.dumps({"completed": len(doc["records"]), "ok": error is None}), flush=True)
            if error:
                break
        if failed:
            break
    if not failed:
        for index, ids in enumerate(sessions):
            expected = "CHECKSUM:" + hashlib.sha256("".join(fixture_result(context, n) for n in range(1, args.turns + 1)).encode()).hexdigest()
            ids["history"].append({"role": "user", "content": "Report exactly this checksum and nothing else: " + expected})
            headers = {"Session_id": ids["session_id"], "Thread-Id": ids["thread_id"],
                       "User-Agent": "cliproxy-cache-probe/1"}
            result = invoke(base, api_key, headers, build_request(args.model, ids["instructions"], ids["history"], ids["cache_key"], "none"))
            result["model"] = args.model
            error = None
            try:
                validate_final_result(result, expected)
            except ValueError as exc:
                error, failed = str(exc), True
            doc["records"].append(response_record(index, "final", result, nonce_hash, error))
            save_progress(output_path, doc)
            if error:
                break
    doc["complete"] = not failed and len(doc["records"]) == args.sessions * (args.turns + 1)
    doc["finished_unix"] = time.time()
    save_progress(output_path, doc)
    return 0 if doc["complete"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sessions", type=int, required=True)
    parser.add_argument("--turns", type=int, required=True)
    parser.add_argument("--context-chars", type=int, required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--manifest", help="Optional mode-0600 file containing generated opaque IDs only")
    parser.add_argument("--execute", action="store_true", help="Actually send requests to local CLIProxyAPI")
    args = parser.parse_args()
    if args.sessions < 2 or args.turns < 1 or args.context_chars < 2048:
        parser.error("sessions must be >= 2, turns >= 1, context-chars >= 2048")
    try:
        return run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
