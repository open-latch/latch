"""Fresh-process prompt benchmark using a disposable vault and real local model.

Run with the installed dependency interpreter and an isolated source checkout::

    python scripts/profile_prompt_hook.py --source CHECKOUT --output-dir PRIVATE_DIR

No production vault or owner is used. Evidence stays outside the checkout.
Stage timings are inclusive (nested stages must not be summed). Interpreter
startup means parent Popen start to the child's first Python timestamp; it
includes process creation and interpreter initialization. Instrumentation adds
bootstrap imports, reported separately. Cold means owner-process cold, not OS
filesystem-cache cold. Concurrency uses one OS account and is not a multi-account
canary. Fixture embeddings and retrieval both use the actual ONNX model.
Owner readiness is measured from the broker election request. Cleanup waits
for that bounded election, even when sampling fails. If startup times out before
publishing any authenticated owner record, a warming process cannot be safely
identified for termination; inspect the private fixture's mcp-daemon.log.
"""
import time

_FIRST_PYTHON_TICK = time.perf_counter()

from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys


PROMPT = "Windows prompt hook latency budget and automatic knowledge retrieval"
TITLE = "Windows prompt hook latency budget"


def child(args):
    sys.path.insert(0, str(Path(args.source) / "src"))
    started = time.perf_counter()
    from latch.hooks import user_prompt_submit as hook
    imported = time.perf_counter()
    stages = {"profiler_bootstrap": (started - _FIRST_PYTHON_TICK) * 1000,
              "hook_import": (imported - started) * 1000}
    calls = {}

    def wrap(obj, name, label=None):
        if not hasattr(obj, name):
            return
        original = getattr(obj, name)
        label = label or name

        def measured(*pos, **kw):
            begin = time.perf_counter()
            try:
                return original(*pos, **kw)
            finally:
                stages[label] = stages.get(label, 0) + (time.perf_counter() - begin) * 1000
                calls[label] = calls.get(label, 0) + 1
        setattr(obj, name, measured)

    original_load = hook._load_runtime
    instrumented_runtime = False

    def load():
        nonlocal instrumented_runtime
        import builtins
        original_import = builtins.__import__

        def timed_import(name, globals=None, locals=None, fromlist=(), level=0):
            measure = name in {"numpy", "latch.retrieval", "latch.store"}
            tick = time.perf_counter()
            try:
                return original_import(name, globals, locals, fromlist, level)
            finally:
                if measure:
                    label = "import_" + name + ("." + ",".join(fromlist) if fromlist else "")
                    stages[label] = stages.get(label, 0) + (time.perf_counter() - tick) * 1000

        begin = time.perf_counter()
        builtins.__import__ = timed_import
        try:
            original_load()
        finally:
            builtins.__import__ = original_import
        stages["retrieval_imports"] = stages.get("retrieval_imports", 0) + (time.perf_counter() - begin) * 1000
        if instrumented_runtime:
            return
        instrumented_runtime = True
        for name in ("connect", "connect_prompt", "connect_current", "_load_vec", "_ensure_schema"):
            wrap(hook.db, name, "sqlite_" + name)
        wrap(hook.embeddings, "embed_remote", "embedding_rpc")
        wrap(hook.embeddings, "embed_remote_result", "embedding_rpc_result")

    hook._load_runtime = load
    for name in ("_mission_control_directive", "_take_cite_nudge", "_vector_path",
                 "_embed_with_bounded_wake", "_emit_and_log", "_print_context"):
        wrap(hook, name)
    if args.budget_ms is not None:
        hook.HARD_BUDGET_MS = args.budget_ms
        os.environ["LATCH_PROMPT_BUDGET_MS"] = str(args.budget_ms)
    sid = f"prompt-profile-{os.getpid()}-{time.time_ns()}"
    hook.read_hook_input = lambda: {"session_id": sid, "cwd": args.source, "prompt": PROMPT}
    recorded = []
    original_write = hook._write_log

    def record(cwd, row):
        recorded.append(dict(row))
        return original_write(cwd, row)

    hook._write_log = record
    buf = io.StringIO()

    class RecordingStdout:
        def write(self, value):
            buf.write(value)
            return sys.__stdout__.write(value)

        def flush(self):
            return sys.__stdout__.flush()

    main_started = time.perf_counter()
    with redirect_stdout(RecordingStdout()):
        code = hook.main()
    stages["main"] = (time.perf_counter() - main_started) * 1000
    output = buf.getvalue()
    print(json.dumps({"first_python_tick": _FIRST_PYTHON_TICK, "stages_ms": stages,
                      "stage_calls": calls, "returncode": code,
                      "actual_fixture_context": TITLE in output and "## KB hits" in output,
                      "output": output, "retrieval_log": recorded[-1] if recorded else {}}))


def setup(args):
    sys.path.insert(0, str(Path(args.source) / "src"))
    from latch.store import db
    from latch.retrieval import embeddings
    conn = db.connect(args.source)
    vector = embeddings.embed(PROMPT)
    node_id = db.insert_node(conn, kind="fact", title=TITLE, body=PROMPT,
                             status="canonical", embedding=embeddings.to_blob(vector))
    conn.close()
    print(json.dumps({"fixture_node_id": node_id, "embedding_dimension": len(vector)}))


def summary(rows):
    import statistics
    totals = sorted(row["total_process_ms"] for row in rows)
    return {"samples": len(rows), "actual_fixture_context": sum(row["actual_fixture_context"] for row in rows),
            "total_process_ms": {"min": min(totals), "median": statistics.median(totals),
                                 "p95_nearest_rank": totals[max(0, int(len(totals) * .95 + .99999) - 1)],
                                 "max": max(totals)}}


@contextmanager
def _using_environment(env):
    """Use the exact child configuration for controller imports and broker calls."""
    original = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _stop_fixture_owner(broker, vault):
    """Stop only a live, authenticated owner of this newly created fixture."""
    import signal

    if broker.runtime_dir().resolve() != vault.resolve():
        raise RuntimeError("Refusing to stop an owner outside the profile fixture")
    owner = broker._checked_discovery()
    if owner is None or not broker._pid_alive(owner["pid"]):
        return
    if not broker.probe_discovery(owner):
        raise RuntimeError("Cannot authenticate fixture owner for cleanup")
    embed = broker.read_live_embed_discovery(owner_payload=owner)
    pid = owner["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 15
    while broker._pid_alive(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError("Fixture owner did not exit during cleanup")
        time.sleep(.05)
    # Windows termination cannot execute the owner's Python finally block.
    # MCP and embedding listeners have different tokens; remove each exact pair.
    broker.remove_discovery_aliases_if_owner(pid=pid, token=owner["token"])
    if embed is not None:
        broker.remove_embed_discovery_if_owner(pid=pid, token=embed["token"])


class _FixtureOwner:
    """Track broker election, including an owner ready after a sample fails."""

    def __init__(self, broker, source, vault):
        self.broker = broker
        self.source = source
        self.vault = vault

    def __enter__(self):
        from concurrent.futures import ThreadPoolExecutor

        if self.broker.runtime_dir().resolve() != self.vault.resolve():
            raise RuntimeError("Broker configuration differs from the profile fixture")
        self.started = time.perf_counter()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = self.executor.submit(self._start)
        return self

    def _start(self):
        owner = self.broker.ensure_daemon(str(self.source), start_reason="prompt_hook")
        return {"ready_ms": (time.perf_counter() - self.started) * 1000,
                "pid": owner["pid"]}

    def wait_ready(self):
        return self.future.result()

    def __exit__(self, exc_type, exc, traceback):
        try:
            # A failed hook sample must not abandon a still-starting owner.
            # Wait for the broker's bounded election before discovering cleanup.
            try:
                self.future.result()
            finally:
                _stop_fixture_owner(self.broker, self.vault)
        finally:
            self.executor.shutdown(wait=True)


def parent(args):
    import hashlib
    import secrets
    import uuid
    source = Path(args.source).resolve()
    output = Path(args.output_dir).resolve()
    if output == source or source in output.parents:
        raise SystemExit("Evidence must be outside the source checkout")
    output.mkdir(parents=True, exist_ok=False)
    test_root = output / "fixture"
    test_root.mkdir()
    capability = secrets.token_hex(32)
    (test_root / ".latch-test-root.json").write_text(json.dumps({
        "format": 1, "root_uuid": str(uuid.uuid4()),
        "capability_sha256": hashlib.sha256(capability.encode()).hexdigest()}), encoding="utf-8")
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("LATCH_", "CLAUDE_KB_")):
            env.pop(key)
    env.update({"LATCH_HOME": str(source), "LATCH_TEST_ROOT": str(test_root),
                "LATCH_TEST_CAPABILITY": capability, "LATCH_KB_DIR": str(test_root / "vaults" / "profile"),
                "PYTHONPATH": str(source / "src")})
    if args.budget_ms is not None:
        env["LATCH_PROMPT_BUDGET_MS"] = str(args.budget_ms)
    with _using_environment(env):
        _profile(args, source, output, test_root, env)


def _profile(args, source, output, test_root, env):
    from concurrent.futures import ThreadPoolExecutor
    import socket
    import sqlite3
    import subprocess
    import threading
    import uuid

    command = [sys.executable, str(Path(__file__).resolve()), "--source", str(source)]
    init = subprocess.run(command + ["--mode", "setup"], env=env, capture_output=True, text=True, timeout=120)
    if init.returncode:
        raise RuntimeError(init.stderr)
    sys.path.insert(0, str(source / "src"))
    from latch.mcp import mcp_broker as broker

    def sample(*, raw=False, base=False):
        child_env = env.copy()
        child_executable = broker._windows_base_command(child_env) if base else sys.executable
        cmd = command + ["--mode", "child"]
        cmd[0] = child_executable
        if args.budget_ms is not None:
            cmd += ["--budget-ms", str(args.budget_ms)]
        sid = "prompt-profile-raw-" + uuid.uuid4().hex
        payload = None
        if raw:
            code = f"import sys; sys.path.insert(0, {str(source / 'src')!r}); from latch.hooks import user_prompt_submit as h; "
            if args.budget_ms is not None:
                code += f"h.HARD_BUDGET_MS = {args.budget_ms}; "
            code += "raise SystemExit(h.main())"
            cmd = [child_executable, "-c", code]
            payload = json.dumps({"session_id": sid, "cwd": str(source), "prompt": PROMPT})
        ready_at_launch = broker.read_discovery() is not None
        begin = time.perf_counter()
        proc = subprocess.run(cmd, env=child_env, input=payload, capture_output=True, text=True, timeout=20)
        elapsed = (time.perf_counter() - begin) * 1000
        if proc.returncode:
            raise RuntimeError(proc.stderr)
        if raw:
            records = []
            for path in (test_root / "vaults" / "profile").glob("retrieve*.log"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    data = json.loads(line)
                    if data.get("sid") == sid:
                        records.append(data)
            row = {"actual_fixture_context": TITLE in proc.stdout and "## KB hits" in proc.stdout,
                   "output": proc.stdout, "retrieval_log": records[-1] if records else {},
                   "instrumented": False}
        else:
            row = json.loads(proc.stdout.splitlines()[-1])
            row["interpreter_startup_ms"] = (row.pop("first_python_tick") - begin) * 1000
            row["instrumented"] = True
        row["total_process_ms"] = elapsed
        row["owner_ready_at_launch"] = ready_at_launch
        row["stderr"] = proc.stderr
        return row

    report = {"source": str(source), "host": socket.gethostname(), "account": os.environ.get("USERNAME"),
              "python": sys.version, "budget_override_ms": args.budget_ms, "fixture": json.loads(init.stdout),
              "boundaries": __doc__, "groups": {}, "owner_readiness": []}
    vault = test_root / "vaults" / "profile"
    cold_rows = []
    for _ in range(args.cold_samples):
        with _FixtureOwner(broker, source, vault) as owner:
            cold_rows.append(sample(raw=True))
            report["owner_readiness"].append(owner.wait_ready())
    report["groups"]["owner_cold"] = cold_rows
    with _FixtureOwner(broker, source, vault) as owner:
        report["owner_readiness"].append(owner.wait_ready())
        report["groups"]["owner_warm"] = [sample() for _ in range(args.samples)]
        report["groups"]["owner_warm_raw"] = [sample(raw=True) for _ in range(args.samples)]
        if os.name == "nt":
            report["groups"]["owner_warm_base"] = [sample(base=True) for _ in range(args.samples)]
            report["groups"]["owner_warm_base_raw"] = [sample(base=True, raw=True) for _ in range(args.samples)]
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            report["groups"]["concurrent_hooks"] = list(pool.map(lambda _: sample(), range(args.samples)))
            report["groups"]["concurrent_hooks_raw"] = list(pool.map(lambda _: sample(raw=True), range(args.samples)))
        locked_rows = []
        for _ in range(args.cold_samples):
            lock = sqlite3.connect(test_root / "vaults" / "profile" / "kb.db", check_same_thread=False)
            lock.execute("BEGIN IMMEDIATE")
            release = threading.Timer(1.0, lock.rollback)
            release.start()
            try:
                locked_rows.append(sample(raw=True))
            finally:
                release.join()
                lock.close()
        report["groups"]["sqlite_writer_contention_raw"] = locked_rows
        report["sqlite_writer_lock_duration_ms"] = 1000
        report["summary"] = {key: summary(rows) for key, rows in report["groups"].items()}
    (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output / "results.json"), "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--mode", choices=("parent", "child", "setup"), default="parent")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--cold-samples", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--budget-ms", type=int)
    args = parser.parse_args()
    if args.mode == "parent" and not args.output_dir:
        parser.error("--output-dir is required")
    {"parent": parent, "child": child, "setup": setup}[args.mode](args)
