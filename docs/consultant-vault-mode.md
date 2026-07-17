# Consultant vault mode (experimental branch)

This is a quarantined CLI-only path for NDA-bound consultant work. It does not
change normal Latch: only a process launched through `latch-vault` receives the
client-local environment.

## Use

Initialize once at the client engagement root (it may contain several repos):

```bash
/path/to/latch/bin/latch-vault init /path/to/client-root
```

Then launch the coding agent from that root or any descendant:

```bash
/path/to/latch/bin/latch-vault claude
/path/to/latch/bin/latch-vault codex
```

The command discovers the initialized root upward from the current directory.
Run `latch-vault status` to print the exact root, binding fingerprint, and
safety posture before starting work.

## Enforced Latch boundary

- The outer/global KB is disconnected. The DB and MCP runtime are pinned to
  `<client-root>/.latch-vault/kb`.
- Latch root logs and maintenance state use
  `<client-root>/.latch-vault/home`.
- temporary files use `<client-root>/.latch-vault/tmp`;
- automatic transcript compaction and Git snapshot/push are disabled;
- every Latch-owned Claude, Codex, or Cursor model subprocess is replaced by a
  local fail-closed blocker because Latch cannot verify its account identity;
- malformed bindings, symlinked writable directories, changed static links,
  missing assets, or an active global Latch kill switch block launch;
- the state directory is mode `0700`, the binding is mode `0600`, and ordinary
  Git clones receive a local `.git/info/exclude` entry.

The configured MCP/hooks continue to execute the installed engine code. The
client-local Latch home contains a private, validated copy of only `schema.sql`
and a validated link to the local embedding-model assets. It deliberately does
not expose seed, compact, install, update, or other standalone CLI scripts.

## Deliberate limits

This is not a VM, container, network sandbox, or provider-account control. The
host Claude Code/Codex account, provider handling, host transcript storage,
shell tools, Git operations, and any software outside Latch remain governed by
the client's policy. If the NDA requires those surfaces to be isolated too, use
a client-controlled account and development environment.

V1 supports Claude Code CLI and Codex CLI only. Desktop/IDE environment
inheritance, live reads from the outer KB, cross-vault import/export, merging,
automatic cleanup, and deployment are intentionally out of scope until the
branch is reviewed and consultant acceptance testing is explicitly approved.
