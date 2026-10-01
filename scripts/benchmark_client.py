"""Compare fresh CLI processes with one persistent, read-only MCP session.

Run with the plugin's package/editor on PYTHONPATH and a live Editor ticking.
No scene writes, capability changes, or automatic retries are performed.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time


CASES = (
    ("status", "host_session_status", {}),
    ("schemas", "operation_schema_list", {"limit": 200}),
    ("query", "operation_query_execute", {"operation": "infernux.runtime.status"}),
    ("batch_20", "operation_batch_execute", {"calls": [
        {"operation": "infernux.runtime.status"} for _ in range(20)
    ]}),
)


def timings(stderr):
    rows = []
    for line in stderr.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "timing":
            rows.append(row)
    return rows


def run(command, *, input=None, timeout):
    started = time.perf_counter()
    result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Benchmark call failed ({result.returncode}): {result.stderr}\n{result.stdout}")
    return (time.perf_counter() - started) * 1000, timings(result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9713/mcp")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 1 <= args.samples <= 50 or not 0 < args.timeout <= 300:
        parser.error("samples must be 1..50 and timeout must be >0 and <=300")
    command = [sys.executable, "-m", "infernux_mcp.client", "--endpoint", args.endpoint,
               "--timeout", str(args.timeout), "--timings"]
    fresh, requests = [], []
    for sample in range(args.samples):
        for name, tool, arguments in CASES:
            elapsed, phases = run(command + ["call", tool, "--args", json.dumps(arguments)], timeout=args.timeout + 15)
            fresh.append({"case": name, "sample": sample, "process_ms": elapsed, "phases": phases})
            requests.append({"id": f"{name}:{sample}", "tool": tool, "arguments": arguments})
    elapsed, phases = run(command + ["session"], input="".join(json.dumps(row) + "\n" for row in requests),
                          timeout=args.timeout * len(requests) + 15)
    calls = [row for row in phases if row["phase"] == "request"]
    if len(calls) != len(requests):
        raise RuntimeError("Persistent session did not report every request")
    report = {
        "samples_per_case": args.samples,
        "fresh_processes": fresh,
        "persistent": {"process_ms": elapsed, "phases": phases},
        "median_ms": {
            name: {
                "fresh_process": statistics.median(row["process_ms"] for row in fresh if row["case"] == name),
                "persistent_request": statistics.median(row["elapsed_ms"] for request, row in zip(requests, calls)
                                                         if request["id"].split(":")[0] == name),
            } for name, _, _ in CASES
        },
        "scope": "Client create includes lazy imports; connect_initialize combines transport and MCP handshake. "
                 "Request includes network, main-thread wait and execution; these are not separated here. "
                 "Fresh process time includes Python startup; persistent request excludes one-time startup/connection. "
                 "No GPU/rendering performance is measured.",
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
