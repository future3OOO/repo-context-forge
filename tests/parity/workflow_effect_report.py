#!/usr/bin/env python3
"""Measure one workflow-effect run per arm from the session rollouts: fresh input tokens,
tool output attributable to repo-source reads, packet size, and the edit/test outcome."""
import json, re, sys
from pathlib import Path

out = Path(sys.argv[1])
READ = re.compile(r"\b(cat|sed -n|nl|head|tail|rg|grep|less|awk)\b.*\.(py|md|sql)\b")
rows, fail = {}, False
for arm in ("base", "cand"):
    rollouts = sorted((out / f"{arm}-sessions").rglob("rollout-*.jsonl"))
    if not rollouts:
        print(f"{arm}: WORKFLOW_EFFECT_NOT_MEASURED (no session)"); fail = True; continue
    fresh = cached = produced = read_bytes = packet_bytes = calls = 0
    for rollout in rollouts:
        pending, usage = {}, {}
        for line in rollout.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") or {}
            if payload.get("type") == "token_count" and payload.get("info"):
                usage = payload["info"].get("total_token_usage") or usage
            if payload.get("type") in ("function_call", "custom_tool_call"):
                args = str(payload.get("arguments") or payload.get("input") or "")
                pending[payload.get("call_id")] = args
                calls += 1
            if payload.get("type") in ("function_call_output", "custom_tool_call_output"):
                args = pending.get(payload.get("call_id"), "")
                output = str(payload.get("output") or "")
                if "REPO_CONTEXT_FORGE_REQUIRED_INTAKE" in output or "bootstrap.py" in args:
                    packet_bytes = max(packet_bytes, len(output))
                elif READ.search(args):
                    read_bytes += len(output)
        # input_tokens is the uncached part; cached_input_tokens is reported separately.
        fresh += usage.get("input_tokens", 0)
        cached += usage.get("cached_input_tokens", 0)
        produced += usage.get("output_tokens", 0)
    diffstat = (out / f"{arm}.diffstat").read_text().strip().splitlines()
    rows[arm] = {"fresh_input_tokens": fresh, "cached_input_tokens": cached, "output_tokens": produced,
                 "source_read_tokens": read_bytes // 4,
                 "packet_tokens": packet_bytes // 4, "tool_calls": calls, "diffstat": diffstat[-1] if diffstat else "no edits"}
for arm, row in rows.items():
    print(arm, json.dumps(row))
if len(rows) == 2:
    b, c = rows["base"], rows["cand"]
    print(f"source-read tokens: base {b['source_read_tokens']} -> cand {c['source_read_tokens']} ({c['source_read_tokens'] - b['source_read_tokens']:+d})")
    print(f"fresh input tokens: base {b['fresh_input_tokens']} -> cand {c['fresh_input_tokens']} ({c['fresh_input_tokens'] - b['fresh_input_tokens']:+d})")
    print(f"packet tokens: base {b['packet_tokens']} -> cand {c['packet_tokens']}")
sys.exit(1 if fail else 0)
