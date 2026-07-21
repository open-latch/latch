#!/usr/bin/env python3
"""Measure shared-daemon scaling with real stdio proxy processes.

Usage:
    .venv/bin/python tests/measure_mcp_resource_scaling.py --sessions 8 --requests 30

The script is diagnostic rather than a platform-fragile CI assertion.  It does
enforce the architectural invariant that every client for one vault/runtime key
reports exactly one heavyweight owner.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_mcp_shared_runtime import McpClient, _stop_daemon  # noqa: E402


def _rss_bytes(pid: int) -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "rss="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return int(result.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _footprint_bytes(pid: int) -> int | None:
    tool = Path("/usr/bin/footprint")
    if not tool.exists():
        return None
    try:
        result = subprocess.run(
            [str(tool), "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Footprint:\s*([0-9.]+)\s*(KB|MB|GB)", result.stdout)
    if not match:
        return None
    scale = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[match.group(2)]
    return int(float(match.group(1)) * scale)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=8)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--compare-legacy", action="store_true")
    args = parser.parse_args()
    if args.sessions < 1 or args.requests < 1:
        parser.error("--sessions and --requests must be positive")

    kb_dir = Path(tempfile.mkdtemp(prefix="latch_mcp_scaling_"))
    clients: list[McpClient] = []
    try:
        started = time.perf_counter()
        for index in range(args.sessions):
            clients.append(McpClient(kb_dir, f"measure-session-{index}"))
        ready_s = time.perf_counter() - started

        statuses = [client.status() for client in clients]
        owner_pids = {int(status["process_pid"]) for status in statuses}
        proxy_pids = [client.process.pid for client in clients]
        owner_pid = next(iter(owner_pids)) if len(owner_pids) == 1 else None

        latencies_ms: list[float] = []
        for index in range(args.requests):
            client = clients[index % len(clients)]
            before = time.perf_counter()
            # latch_embed was removed from the tool surface; latch_search is a
            # representative daemon request that also exercises the shared
            # embedding owner (it embeds the query).
            response = client.call_tool(
                "latch_search", {"query": f"latency sample {index % 5}", "limit": 1}
            )
            latencies_ms.append((time.perf_counter() - before) * 1000.0)
            if response is None:
                raise AssertionError("latch_search returned no result")

        proxy_rss = [_rss_bytes(pid) for pid in proxy_pids]
        proxy_footprints = [_footprint_bytes(pid) for pid in proxy_pids]
        result = {
            "sessions": args.sessions,
            "requests": args.requests,
            "startup_to_all_ready_s": round(ready_s, 4),
            "unique_heavy_owner_pids": sorted(owner_pids),
            "heavy_owner_count": len(owner_pids),
            "owner_rss_bytes": _rss_bytes(owner_pid) if owner_pid else None,
            "owner_footprint_bytes": _footprint_bytes(owner_pid) if owner_pid else None,
            "proxy_rss_bytes": proxy_rss,
            "proxy_footprint_bytes": proxy_footprints,
            "summed_proxy_rss_bytes": sum(value for value in proxy_rss if value is not None),
            "summed_proxy_footprint_bytes": sum(
                value for value in proxy_footprints if value is not None
            ),
            "request_latency_ms": {
                "mean": round(statistics.fmean(latencies_ms), 4),
                "p50": round(_percentile(latencies_ms, 0.50), 4),
                "p95": round(_percentile(latencies_ms, 0.95), 4),
                "max": round(max(latencies_ms), 4),
            },
            "runtime_key": statuses[0]["daemon"]["runtime_key"],
        }
        if args.compare_legacy:
            legacy_dir = Path(tempfile.mkdtemp(prefix="latch_mcp_legacy_"))
            legacy: McpClient | None = None
            try:
                legacy = McpClient(legacy_dir, "legacy-measure", force_legacy=True)
                legacy_latencies: list[float] = []
                for index in range(args.requests):
                    before = time.perf_counter()
                    response = legacy.call_tool(
                        "latch_search", {"query": f"latency sample {index % 5}", "limit": 1}
                    )
                    legacy_latencies.append((time.perf_counter() - before) * 1000.0)
                    if response is None:
                        raise AssertionError("legacy latch_search returned no result")
                shared_p95 = _percentile(latencies_ms, 0.95)
                legacy_p95 = _percentile(legacy_latencies, 0.95)
                result["legacy_baseline"] = {
                    "process_rss_bytes": _rss_bytes(legacy.process.pid),
                    "process_footprint_bytes": _footprint_bytes(legacy.process.pid),
                    "request_latency_ms": {
                        "mean": round(statistics.fmean(legacy_latencies), 4),
                        "p50": round(_percentile(legacy_latencies, 0.50), 4),
                        "p95": round(legacy_p95, 4),
                        "max": round(max(legacy_latencies), 4),
                    },
                    "shared_p95_delta_ms": round(shared_p95 - legacy_p95, 4),
                    "shared_p95_delta_percent": round(
                        ((shared_p95 - legacy_p95) / legacy_p95 * 100.0)
                        if legacy_p95
                        else 0.0,
                        2,
                    ),
                }
            finally:
                if legacy is not None:
                    legacy.close()
                shutil.rmtree(legacy_dir, ignore_errors=True)
        if result["owner_footprint_bytes"] is not None:
            result["summed_runtime_footprint_bytes"] = (
                result["owner_footprint_bytes"] + result["summed_proxy_footprint_bytes"]
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        if len(owner_pids) != 1:
            print("FAIL: more than one heavyweight owner", file=sys.stderr)
            return 1
        return 0
    finally:
        for client in clients:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
