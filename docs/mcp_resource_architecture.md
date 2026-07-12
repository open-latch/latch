# Bounded latch MCP runtime — issue 1465

Status: public draft PR #21 on `mcp-resource-lifecycle`. Independent review of
head `c5f9954` found that protocol-compatible aliases fragmented the proxy cap
and that pre-registry proxies still timed out generically. The current
remediation passes full local verification; the PR remains intentionally
draft pending three-OS receipts, independent merge-trust review, and
real Codex, Claude Code, and Cursor lifecycle dogfood.
Measurements were collected on macOS 13.5, Apple Silicon, on 2026-07-10.

## Decision

Use one lazily elected, warm MCP daemon per pinned latch vault and runtime
fingerprint. Keep the existing stdio configuration, but make each host-created
stdio process a standard-library-only proxy that forwards MCP JSON-RPC to that
daemon.

This gives latch two resource bounds:

1. Heavyweight state is constant per vault/current owner key: one FastMCP registry,
   one ONNX `InferenceSession`, one tokenizer, and one hook embed listener.
2. Lightweight proxies use an LRU lease pool. The default cap is 32. A proxy
   retires itself only when the pool is over cap, it has been idle for five
   minutes, and it has no in-flight request. No process signals or kills a peer.

The second bound is intentionally soft while more than 32 proxies are genuinely
active. Evicting an in-flight MCP connection would be a correctness regression.
Once excess connections become idle, steady state returns to the configured
cap. A host that does not reconnect a retired stdio server requires a fresh
task for that old context, which is preferable to unbounded machine pressure.
At the measured macOS footprints, the default one-owner-plus-32-proxy steady
ceiling is roughly 600 MB per vault/owner scope rather than an unbounded
multiple of 126–166 MB servers.

## Root cause, reproduced

The observed failure has two independent layers.

### Host lifecycle

The Codex app server creates separate stdio MCP children for tasks and subagent
contexts and retains many of them. In the live sample, 14 open-latch
`src/mcp_server.py` processes were direct children of one Codex app-server PID.
Start-time clusters aligned with task/subagent activity. No recursive latch MCP
children, compactors, or self-heal jobs existed beneath them.

This is consistent with reports in the public `openai/codex` repository about
per-thread MCP managers and retained stdio children, including
[#11324](https://github.com/openai/codex/issues/11324) and
[#18333](https://github.com/openai/codex/issues/18333). Those reports are
corroboration; the live process tree is the evidence for this machine.

Codex's current documentation says a stdio MCP server is a command-started local
process and also supports Streamable HTTP servers, but it does not promise that
an exited stdio server will be restarted inside an old task. See the official
[Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

### Latch ownership

Every old latch MCP process did all of the following at startup:

- imported FastMCP, NumPy, ONNX Runtime, tokenizers, and latch's full tool graph;
- bound its own loopback embed listener;
- overwrote the same pinned-vault `embed.sock.json` discovery file; and
- synchronously loaded and warmed its own ONNX model.

Only the newest listener remained discoverable. Older listeners and models
stayed resident but were stranded from hook traffic. If the discovery-file
owner died, clients failed rather than electing one of the older owners or
starting a replacement.

Measured live state:

| Evidence | Result |
|---|---:|
| Open-latch MCP children | 14 |
| Loopback listeners | 14 |
| Summed macOS process footprint | 2,238 MB |
| Machine swap in use | 3,121 MB |
| Controlled empty Python footprint | 6.4 MB |
| FastMCP import footprint | 42 MB |
| Full latch server import, no model | 60 MB |
| Controlled loaded model process | 102 MB |
| Controlled running legacy server | 126 MB |

RSS alone understated old processes after macOS compressed or swapped their
private pages. Representative `vmmap -summary` results showed about 112–121 MB
allocated per loaded process and up to 153 MB swapped for an individual stale
process.

ONNX Runtime documents allocator and thread-pool sharing between multiple
sessions in one process. It does not make separate Python processes share an
`InferenceSession`; its arena documentation also notes that arena allocations
normally remain owned by a session. See ONNX Runtime's
[memory guidance](https://onnxruntime.ai/docs/performance/tune-performance/memory.html),
[threading guidance](https://onnxruntime.ai/docs/performance/tune-performance/threading.html),
and [C API discussion of shared pools and allocators](https://onnxruntime.ai/docs/get-started/with-c.html).

## Alternatives considered

| Option | Memory behavior | Latency/reliability | Decision |
|---|---|---|---|
| Keep current servers; require occasional app restart | Linear heavy memory until restart | No code risk, poor user experience | Reject |
| Lazy model load in every stdio process | Helps never-used sessions; active sessions remain linear | First-use cold latency per task | Reject as incomplete |
| Unload per-process ONNX after idle | Still duplicates active state; allocator release is unreliable | Reload churn and tail latency | Reject |
| Share only the embed model | One model, but FastMCP/full server import remains about 60 MB per task | Easy recovery, only partial bound | Reject as final architecture |
| Rely on ONNX shared allocators or shared initializers | Useful only for sessions in one process | Does not solve host-created processes | Reject |
| Configure a direct Streamable HTTP MCP server | Standard multi-client shape and no stdio proxies | Requires a separately supervised service and loses current per-process cwd/session signals | Keep as a future adapter option |
| Tiny stdio proxy plus shared daemon | One heavy owner; about 14–15 MB per host context | Existing configs work; fixed low-ms forwarding cost | Select |

The MCP specification explicitly defines stdio as a client-launched subprocess
and Streamable HTTP as an independent process able to handle multiple clients.
It also permits a stdio server to initiate shutdown by closing output and
exiting. See the official
[transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and [lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
sections.

## Runtime design

### Ownership and discovery

- `src/mcp_server.py` intercepts direct execution before importing FastMCP or
  ONNX and enters `mcp_proxy.py`. Existing installer/config paths do not change.
- The first proxy acquires an atomic start lock and launches `mcp_daemon.py`.
  Concurrent proxies wait for the same owner.
- Before ownership fencing or heavyweight imports, the detached daemon checks
  the proxy's transport protocol and explicit lifecycle-capability epoch. A
  newer incompatible proxy receives an actionable fresh-task failure. A daemon
  then acquires an OS-released process-lifetime fence for its vault/current
  runtime key. Broker death or a slow-start timeout can launch a contender, but
  the contender exits before model loading; only the fenced owner may warm or
  publish normal discovery.
- Discovery and election locks live in a runtime-keyed registry beneath the
  pinned vault. Files are atomically replaced and mode 0600. The daemon binds
  only `127.0.0.1`; connections authenticate with a 256-bit random token.
  Current and alias discovery are published only after synchronous runtime and
  model initialization completes, so probes cannot observe a listener that is
  bound but not ready to serve.
- A runtime key content-fingerprints the internal transport version, relevant
  source and tokenizer/config files, plus model size. It deliberately excludes
  mtimes, so identical trees have identical keys. The 90 MB model is not hashed
  by every proxy; a same-size incompatible model replacement must bump the
  protocol version. During an in-place compatible upgrade, retained old-key
  capability-epoch proxies receive an authenticated discovery alias to the
  single current owner. They adopt `owner_runtime_key` and migrate their lease
  write-first into that owner's aggregate capacity pool. They do not create one
  heavyweight compatibility owner or one capacity pool per historical key.
  Proxies from before the capability epoch—including the pre-registry `fa162bd`
  layout—receive a bounded MCP error requiring a fresh task. The daemon
  completes only their replayed initialization; it rejects the real deferred
  request before FastMCP, so no mutation has an unknown outcome.
  Cleanup scans aliases and removes only records whose PID and token still
  match, so an old owner cannot erase a newer owner's record.
- POSIX startup double-forks before heavyweight imports and the proxy waits for
  the bootstrap child. This prevents reclaimed daemons becoming zombies under
  long-lived proxies. Windows uses detached-process creation flags.

### Connection isolation

Each proxy sends an authenticated prelude containing cwd, session identity and
source, proxy PID, connection ID, and runtime key. The daemon binds these values
to `contextvars`; concurrent FastMCP sessions therefore use their own cwd and
session attribution even though tool code runs in one process.

Codex SessionStart markers are now keyed by canonical workspace beneath the
pinned vault. This fixes the prior failure where a single pinned marker could
attribute repo A's MCP work to repo B's newest task. Direct `LATCH_SESSION_ID`,
`CLAUDE_CODE_SESSION_ID`, or `CODEX_THREAD_ID` remains authoritative.

The current live Codex MCP child environment did not contain `CODEX_THREAD_ID`,
despite the public Codex issue history describing that variable. The workspace
marker preserves the existing fallback, but same-workspace parallel tasks
cannot be mapped to distinct Codex transcript IDs without a host-provided
signal. Latch now reports the attribution source and never accepts a marker
whose `project_path` differs from the connection cwd. This limitation is
visible rather than silently misattributed.

### Recovery and reclamation

- The daemon defaults to a 60-minute global idle TTL. It does not reclaim while
  any request is in flight, and prompt-hook embed traffic refreshes activity so
  a task that is still being used is not mistaken for an idle owner.
- Proxies remain alive after daemon reclamation. On the next host message, they
  elect/reconnect to an owner and replay only MCP initialization.
- After an in-place compatible upgrade, a capability-epoch proxy's old key is
  aliased to the current owner, its initialization is replayed there, and its
  lease moves into the owner's pool. A pre-capability or pre-registry proxy is
  rejected with a bounded fresh-task message; it never degrades into a generic
  readiness timeout or silently joins semantics it cannot enforce.
- The prompt hook requests a single-flight background wake within its 250 ms
  wall. If the owner is not ready, it emits an explicit "not similarity-scored"
  receipt instead of falsely reporting a below-floor result.
- If the owner dies during a tool call, the proxy reports an unknown outcome
  and directs the caller to inspect current state before deciding on a new
  operation. It never automatically replays a potentially mutating call.
- Proxy leases are individual files scoped to the current owner key, not a
  contended shared registry. The cap is therefore 32 per vault/owner scope;
  compatible retained aliases participate in the same capacity and retirement
  decisions. Migration writes the new lease before removing the old, and scans
  deduplicate by connection ID if a process dies between those operations.
  Every proxy updates only its
  own lease and retires only itself. Lease identity is deliberately PID-only to
  avoid a second OS-specific process-inspection subsystem in every lightweight
  proxy. PID reuse can therefore make a dead row look live until its heartbeat
  crosses the five-minute stale threshold. That is the accepted bound: after
  five minutes the row is excluded from capacity but preserved, because an
  observer cannot prove whether the original proxy was merely suspended. The
  stale diagnostic can persist until that PID exits. Pre-capability alias and
  pre-registry leases are excluded from the enforceable pool because those
  binaries cannot implement its retirement contract, but status and doctor
  count them explicitly and require fresh tasks instead of hiding them. Peers
  are never signaled or killed. Each capable lease
  also persists when that proxy first observed over-cap pressure, so runtime
  status can report the current sustained duration without introducing a
  shared pressure registry.
- Default proxy policy: cap 32, five-minute minimum idle before over-cap
  retirement, 30-second heartbeat. Set `LATCH_MCP_PROXY_CAP=0` to disable the
  bound for diagnosis.

### Diagnostics

`latch_runtime_status` / `kb_runtime_status` reports:

- current mode (`shared_daemon` or `legacy_stdio`);
- daemon PID, runtime key, uptime, idle TTL, active connections, and in-flight
  requests;
- current proxy PID, cwd, session ID, and attribution source;
- owner-scoped proxy cap/idle/heartbeat policy, compatible alias count, capable
  live/stale leases, and separately labeled incompatible legacy lease counts;
- model-loaded state and embed-listener owner PID/port;
- approximate peak RSS without exposing authentication tokens;
- a bounded 24-hour lifecycle summary covering starts, idle exits, degraded
  prompts, stale/over-cap lease state, retirements, reconnects, incompatible
  upgrades, and failures;
- daemon start reason, cold-start duration, and peak concurrent connections;
  and
- both completed retirement duration and the current over-cap duration. The
  current value is explicitly a lower bound from live proxies' first
  observations, not a fabricated host-global timestamp.

`latch doctor` warns on recent lifecycle pressure, stale live leases, dead
discovery, incompatible historical leases, and explicit legacy fallback. It
also warns when 24-hour lease
high-water reaches 75% of the live configured cap (24 at the default 32) or
while current over-cap duration is non-zero. Lifecycle JSONL is transition-only:
it records no prompt text, tool arguments, authentication tokens, or per-request
traffic.

Daemon reconnect success/failure is observable inside a retained proxy. A
proxy that retires cannot prove that its host restarted the same task: the old
process has exited, and current hosts do not provide a stable cross-process
connection id on every adapter. Latch therefore emits an actionable retirement
WARN telling the operator to confirm reconnect or start a fresh task; it does
not invent causal telemetry from an unrelated later proxy start. Start reason
`daemon_reconnect` similarly means the retained proxy needed a new owner;
correlate it with a preceding `daemon_idle_exit` to distinguish planned idle
reclamation from an owner crash.

## Prototype results

The 14-session test uses real stdio proxy processes and real MCP initialize,
tool-list, tool-call, and embedding paths.

| Metric | Legacy/live baseline | Shared prototype |
|---|---:|---:|
| Sessions | 14 | 14 |
| Heavy model owners | 14 | 1 |
| Summed process footprint | 2,238 MB | 362 MB |
| Reduction | — | 83.8% |
| Proxy footprint | n/a | 14 MB each |
| All 14 clients ready, sequential launch | not captured | 0.86 s |
| Shared embed p95 at 14 clients | — | 8.08 ms |

An isolated warm microbenchmark measured shared `latch_embed` p95 at 7.94 ms
versus 7.17 ms through a legacy process: +0.77 ms absolute. This is the fixed
proxy/loopback cost on an unusually small tool call. The UserPromptSubmit hook
does not traverse the stdio proxy; it continues to use the daemon's embed
listener directly. The existing hook benchmark remained below its 250 ms
budget: 114.3 ms median and 179.9 ms maximum wall time in the fresh measured run.

The same text embedded through separate proxies produced vectors equal within
`1e-7`; retrieval representation and quality are unchanged.

### Why 32 remains the candidate cap

The original live failure showed 14 retained contexts, so 32 is not presented
as a discovered universal concurrency limit. It is a deliberately conservative
first-release candidate: 2.3× the observed count, while still giving the
lightweight side a finite steady-state bound. A fresh 32-client production-path
run on 2026-07-11 kept exactly one heavy owner, initialized all clients in
1.37 seconds, measured 5.86 ms embed p95 over 64 calls, and used about 590 MB of
summed macOS footprint. No active client was retired.

The cap should be revisited from lifecycle evidence rather than silently
degrading: `proxy_high_water`, `proxy_over_cap`, current/completed over-cap duration,
`proxy_retired`, and degraded/reconnect events are summarized by runtime status
and doctor. High-water at 75% of the configured cap is a WARN; at the default,
that is 24 leases. Repeated high-water near 32 with long genuinely-active
overlap is a reason to raise the cap or revisit host integration;
retained-but-idle excess is exactly what the five-minute retirement policy is
designed to absorb.

## Verification

Latest blocker-remediation receipts: 55 focused lifecycle/doctor/Codex/embed
tests and 921 hermetic tests passed locally; changed-file Ruff, public-release
hygiene, compilation, and `git diff --check` passed. Warm prompt-hook wall was
108.5–135.5 ms. Fourteen clients used one owner at 1.03 s readiness and 10.33 ms
embed p95; 32 used one owner at 1.73 s and 8.10 ms p95. The 14-client tail is
reported as an observation, not a hard upper bound. Content fingerprinting
adds only proxy-start work; it does not run per request or enter the prompt-hook
embed path.

The named deletion pass retained the four defensible boundaries—standard-library
broker, stdio proxy, heavyweight owner, and cycle-free connection context—and
added no module or dependency. The runtime core is now 2,284 physical / 2,013
nonblank lines, 283 physical lines above `c5f9954`. The increase implements the
capability handshake, legacy rejection session, owner-pool aggregation, and
lease migration; no parallel capacity state machine or production fault seam
was added. That size remains an explicit independent-review target rather than
being presented as inherently minimal.

The production-representative tests cover:

- concurrent clients sharing exactly one model owner;
- connection-local session attribution;
- identical embeddings across clients;
- owner crash and re-election;
- idle owner reclamation and lazy recreation;
- v1 → v2 → v1 blue/green discovery isolation;
- a real post-commit/lost-response mutation with no replay or retry advice;
- prompt-hook wake and truthful bounded degradation after idle exit;
- stale-heartbeat capacity exclusion with an explicit five-minute PID-reuse bound;
- retained-proxy recovery across an in-place compatible source upgrade;
- lease migration and aggregate over-cap pressure across compatible old/new
  runtime keys;
- bounded fresh-task rejection through real `7bcb86d` and pre-registry
  `fa162bd` proxy snapshots;
- no current or alias discovery publication before runtime initialization;
- incompatible-protocol rejection before ownership fencing/heavy imports;
- configured-cap-derived 75% doctor warnings and sustained pressure duration;
- startup reason/cold duration plus daemon peak-connection accounting;
- LRU over-cap proxy self-retirement without killing peers; and
- distinct Codex markers for different workspaces sharing one pinned vault.

Commands:

```bash
.venv/bin/python -m pytest -q tests/test_mcp_lifecycle_contract.py tests/test_mcp_shared_runtime.py tests/test_doctor.py tests/test_codex_session.py tests/test_embed_daemon.py
.venv/bin/python tests/measure_mcp_resource_scaling.py --sessions 14 --requests 50
.venv/bin/python tests/measure_mcp_resource_scaling.py --sessions 32 --requests 64
.venv/bin/python tests/measure_mcp_resource_scaling.py --sessions 8 --requests 100 --compare-legacy
.venv/bin/python tests/measure_hook_latency.py
```

## Rollout and reversal

No configuration migration is required: installed hosts already execute
`src/mcp_server.py`, which now enters the lightweight proxy before heavyweight
imports. Existing, already-running legacy MCP processes retain old code until
their host context exits or the host is restarted.

Recommended rollout after review:

1. Run focused and full tests on macOS, Linux, and Windows.
2. Dogfood one fresh Codex task and one parallel/subagent wave; inspect
   `latch_runtime_status` and process footprint.
3. Dogfood Claude Code and Cursor lifecycle behavior, especially host handling
   after an intentionally over-cap idle proxy exits.
4. Keep default cap 32 during the first release; tune only from observed active
   concurrency, not process-leak counts.
5. Keep legacy startup explicit-only; do not silently restore the old heavy
   topology when shared-owner startup fails.

Reversal is immediate and local:

- set `LATCH_MCP_FORCE_LEGACY=1` to use the old per-process server path;
- set `LATCH_MCP_PROXY_CAP=0` to disable proxy retirement while retaining the
  shared owner;
- stop the daemon PID shown by `latch_runtime_status`; the next proxy recreates
  it; or
- revert the `mcp_server.py` early dispatch and new runtime modules.

Broker startup failure is visible and fails closed by default. Operators can
set `LATCH_MCP_ALLOW_LEGACY_FALLBACK=1` for an explicit temporary fallback;
`LATCH_MCP_FORCE_LEGACY=1` remains the direct diagnostic override.

## Remaining boundary

One heavyweight owner is keyed per pinned vault and current runtime fingerprint;
capability-epoch retained keys are discovery aliases and one aggregate lease
pool, not additional model owners or hidden capacity pools.
Explicitly running many named vaults can still load several models. That is a
deliberate isolation boundary. If real multi-vault use makes this material, the
next step is a host-wide pure-embedding service keyed by model fingerprint with
a bounded LRU model cache; it should not be added before that usage exists,
because the current single-vault architecture already removes the observed
session/subagent multiplier.
