# latch

Latch is an open-source developer tool for preserving project context and
decisions across AI coding sessions.

This repository is an early source snapshot. It keeps the basic local
mechanics: a SQLite knowledge base, MCP tools for reading and writing it,
session compaction, and a simple gate that can surface prior decisions or
rejected paths before an agent repeats old work.

## What is included

- Local SQLite KB schema, migrations, node/edge storage, status handling, and
  artifact coordinates.
- Search and read primitives: `kb_search`, `kb_get`, `kb_recent`, and
  `kb_verify`.
- Write primitives: `kb_insert`, `kb_update`, `kb_append`, `kb_link`,
  `kb_unlink`, and structured correction helpers.
- Decision capture: `kb_capture_decision` plus structural decision/adversary
  logs that avoid storing raw prompt text.
- Session compaction for carrying useful session context into the KB.
- A gate path, `kb_gate`, for checking a request against stored decisions,
  rejected paths, constraints, and recent context.
- Minimal Claude Code and Codex setup scripts, doctor scripts, and command
  wrappers for `/kb-compact` and `/kb-gate`.
- Tests around the core storage, search, compaction, gate, seed, and installer
  paths.

## What is intentionally left out

- Higher-level planning/reporting commands.
- Extra editor or app connector work.
- Polished onboarding flows.
- Packaged releases.

The point of this snapshot is to make the core idea inspectable: agents can use
a small local KB to remember why decisions were made, what has already been
ruled out, and what should be checked before changing code.

## Quick Start

Use Python 3.11 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Run a basic local check:

```bash
bash bin/latch_doctor.sh
```

For Claude Code setup:

```bash
bash bin/install_engine.sh
bash bin/install_commands.sh
```

For Codex setup:

```bash
bash bin/install_codex.sh
bash bin/latch_codex_doctor.sh
```

After setup, restart the agent session so the MCP tools are available.

## Basic Workflow

Search recent project context:

```text
kb_search("database decision")
kb_get(12)
kb_recent(limit=5)
```

Capture a decision:

```text
kb_insert(
  kind="decision",
  title="Keep the local SQLite store",
  body="Use SQLite for the first local KB because it keeps setup simple.",
  status="canonical",
)
```

Compact a session into the KB:

```text
/kb-compact
```

Check a request against prior context:

```text
/kb-gate "Replace the local SQLite store with a remote service"
```

The gate is not a policy engine. It is a context check: it reads stored project
decisions and returns a recommendation with cited KB evidence.

## Repository Map

- `src/db.py`, `src/schema.sql`: local KB schema and persistence helpers.
- `src/search.py`, `src/embeddings.py`: retrieval primitives.
- `src/mcp_server.py`: MCP tool surface.
- `src/compactor.py`, `src/codex_compact.py`: session-summary paths.
- `src/gate.py`: request checking against KB context.
- `src/seed.py`: first-pass decision/context extraction from local transcripts.
- `bin/`: setup, doctor, compact, gate, and status wrappers.
- `commands/`: small slash-command wrappers for compact and gate.
- `tests/`: focused tests for the local primitives.

## Development Checks

Useful smoke checks:

```bash
python -m py_compile src/db.py src/search.py src/mcp_server.py src/compactor.py src/gate.py
python tests/test_gate.py
python tests/test_verify.py
python tests/test_run_compact_now.py
python tests/test_run_kb_gate_wrapper.py
```

This snapshot is meant to be easy to replace as the public surface gets clearer.
