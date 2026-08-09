# Codex-attribution corpus fixtures

These JSONL files are sanitized derivatives of the two live Codex canary
rollouts named by Latch decision 4666. They preserve the observed current-host
record sequence and wrapping:

1. `session_meta`
2. the outer `exec` `custom_tool_call`
3. its asynchronous `custom_tool_call_output`
4. the subsequent `wait` call
5. `event_msg` / `mcp_tool_call_end`, with the gate result encoded as the JSON
   string at `payload.result.Ok.content[0].text`

Requests, filesystem and project identity, session/call ids, proof identity,
runtime identity, evidence ids, and result prose were replaced or removed.
Key order, JSONL encoding, scalar types, the first canary's explicit
`host_adapter` and the second canary's null `host_adapter` were preserved.

The fixtures derive from source records 1, 16, 17, 20, and 21 of the first
2026-08-07 rollout and records 1, 14, 15, 18, and 19 of the second. Their byte
hashes are pinned in `manifest.json`. They are additive attribution fixtures;
they do not modify the frozen outcome-measurement fixture pack.
