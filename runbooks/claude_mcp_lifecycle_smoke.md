# Claude Code shared-MCP lifecycle smoke proof

Use this runbook to dogfood the shared-runtime lifecycle from a real **Claude
Code** session: prove Claude is attributed correctly, that one heavy model owner
is shared, that an idle-reclaimed daemon transparently reconnects on the next
tool call, and that an over-cap idle proxy retires itself. This is the Claude
counterpart to `cursor_gate_smoke.md`. Executing every step and retaining the
listed transition evidence is intended to close the Claude reconnect/retirement
dogfood blocker on PR #21 ("Share one Latch MCP runtime across agent sessions").
The existence of this runbook is not itself a validation receipt or a claim that
the blocker is closed.

The runtime is host-agnostic — the same daemon/proxy serves Codex and Cursor —
but this proof pins the **Claude** path end to end because Claude attribution is
env-var based (`CLAUDE_CODE_SESSION_ID`), with no SessionStart-marker fallback.

## Success criteria

- `latch_runtime_status` reports `mode: shared_daemon` with
  `heavy_model_owner_count: 1`.
- The live connection shows `session_source: env:CLAUDE_CODE_SESSION_ID` and a
  `session_id` equal to this Claude Code session's id (not `unavailable`, not a
  Codex thread id).
- Opening a second Claude task in the same vault does **not** start a second
  heavy owner: `heavy_model_owner_count` stays `1` and `proxy_pool.live_leases`
  increments instead.
- After the daemon idle-exits, the next `latch_*` MCP call still succeeds; a
  `daemon_reconnect_succeeded` lifecycle event is recorded (no fresh task
  needed).
- An in-flight MCP call is never silently retried across a daemon disconnect:
  the caller sees an explicit unknown-outcome error, not a replayed mutation.
- When the proxy pool is over cap, an idle proxy retires itself after the retire
  window and emits `proxy_retired`; active/in-flight proxies are never evicted.
- `bin/latch_doctor.sh` reports the MCP runtime lifecycle check as `OK` (or a
  truthful pressure warning), never a crash.

## Lifecycle knobs

Claude launches the MCP server from its MCP config (`mcp_server.py`, which
delegates to the shared proxy). To make idle-exit and retirement observable in a
short session, add `daemon_idle_ttl_s: 10` to the pinned vault's
`runtime_settings.json`. Set the proxy-only values in the Claude MCP server
entry's `env`. Defaults are production-scale; these overrides are for the proof
only.

| Setting | Scope | Default | Proof value | Effect |
|---|---|---|---|---|
| `daemon_idle_ttl_s` | vault JSON | 3600 | `10` | daemon idle-exits ~10s after the last activity |
| `LATCH_MCP_PROXY_CAP` | proxy env | 32 | `1` | pool is "over cap" with 2+ concurrent proxies |
| `LATCH_MCP_PROXY_RETIRE_IDLE_SEC` | proxy env | 300 | `10` | an over-cap idle proxy retires ~10s after going idle |
| `LATCH_MCP_PROXY_HEARTBEAT_SEC` | proxy env | 30 | `5` | leases refresh faster so status reflects reality sooner |

Preserve the autonomous-maintenance keys already in `runtime_settings.json`.
Do not ship these proof overrides; remove the TTL key and proxy env overrides
afterward.

## 1. Confirm shared runtime + Claude attribution

From the hooked Claude Code session, call the MCP tool `latch_runtime_status`
(or run `bin/latch_doctor.sh`). Confirm:

```jsonc
{
  "mode": "shared_daemon",
  "connection": { "session_source": "env:CLAUDE_CODE_SESSION_ID", "session_id": "<this session>" },
  "daemon": { "session_sources": { "env:CLAUDE_CODE_SESSION_ID": 1 }, "active_connections": 1 },
  "embedding": { "model_loaded": true, "heavy_model_owner_count": 1 },
  "proxy_pool": { "cap": 1, "live_leases": 1, "bounded": true }
}
```

If `mode` is `legacy_stdio`, the shared runtime is not active — run
`bin/latch_doctor.sh` and do not treat legacy mode as a pass. If
`session_source` is `unavailable`, Claude did not inject `CLAUDE_CODE_SESSION_ID`
into the MCP subprocess; capture that as a blocker (Claude has no marker
fallback).

## 2. Prove one owner across parallel Claude tasks

Open a second Claude Code task in the **same** vault and issue any `latch_*` call
in it. Re-check `latch_runtime_status` from either task:

- `embedding.heavy_model_owner_count` remains `1`.
- `daemon.active_connections` / `proxy_pool.live_leases` increases to `2`.
- `daemon.peak_connections` reflects the peak.

Two heavy owners for one vault+runtime fingerprint is a failure.

## 3. Prove idle reclamation + transparent reconnect

With vault `daemon_idle_ttl_s` set to `10`:

1. Make one MCP call, then leave the session idle (no MCP calls) for ~15s.
2. The owner should idle-exit. Confirm a `daemon_idle_exit` lifecycle event
   (visible via the lifecycle counts in `latch_runtime_status`, or the
   `mcp_lifecycle-*.log` stream under the project runtime dir).
3. Now make another `latch_*` call. It must **succeed without a fresh task** —
   the resident proxy reconnects and replays MCP initialization.
4. Confirm a `daemon_reconnect_succeeded` event followed the call.

The proxy staying resident across an owner idle-exit, then serving the next call,
is the core recovery guarantee.

## 4. Prove in-flight calls are never blindly retried

This proves the "ambiguous mutation is never replayed" invariant. It is a
negative test — you are proving nothing silently double-commits.

1. Start a mutating MCP call (e.g. `latch_insert`).
2. While it is in flight, kill the owner daemon (`daemon.pid` from
   `latch_runtime_status`): `kill <pid>`.
3. The caller must receive an explicit error stating the outcome is unknown and
   was **not** replayed — never a silently retried write.
4. Inspect KB state and confirm the node was written **at most once** (no
   duplicate from a replay).

## 5. Prove over-cap idle self-retirement

With `LATCH_MCP_PROXY_CAP=1` and `LATCH_MCP_PROXY_RETIRE_IDLE_SEC=10`:

1. Have two Claude tasks connected (2 live leases, cap 1 → over cap).
2. Leave the **less recently active** proxy's task idle for ~15s.
3. That proxy should retire itself and emit `proxy_retired`
   (`reason=idle_over_cap`); its host prints a `retiring idle over-cap MCP proxy`
   notice. The most-recently-active proxies within the cap are retained.
4. A proxy with an in-flight or replaying request must **not** retire — verify by
   keeping one task busy and confirming it survives.

## 6. Doctor + verification

```bash
bin/latch_doctor.sh
```

The MCP runtime lifecycle check should report the owner, live/stale/legacy lease
counts, and 24h high-water, and be `OK` or a truthful pressure warning — never a
traceback. Restore production env values (remove the proof overrides) afterward.

## Capture

Retain the proof under a dated directory outside the repo: the four
`latch_runtime_status` snapshots (steps 1–3, 5), the doctor output, the relevant
`mcp_lifecycle-*.log` transition rows (`daemon_idle_exit`,
`daemon_reconnect_succeeded`, `proxy_retired`, and any
`daemon_disconnect_unknown_outcome` from step 4), and the step-4 caller error.
Do not save prompt text, tool arguments, or tokens — the lifecycle stream is
transition-only by design and the capture must match.

## Boundaries

This runbook proves the shared-runtime **lifecycle** for the Claude host. It does
not re-prove gate enforcement or compaction (host-agnostic, covered elsewhere).
Claude attribution depends on `CLAUDE_CODE_SESSION_ID` being present in the MCP
subprocess env; there is intentionally no marker fallback for Claude, so a
missing env var surfaces as `session_source: unavailable` rather than a guessed
id.
