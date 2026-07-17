# Consultant vault mode (experimental branch)

This is a quarantined CLI-only path for NDA-bound consultant work. A repository
is unaffected until its client root is explicitly initialized. Initialization
creates a persistent fail-closed tripwire: from then on, every ordinary Latch
entry point under that root refuses the outer KB unless the process has the
exact active vault binding.

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
Launch must happen inside that root; an outside `--root` launch is rejected.
Run `latch-vault status` to print the exact root, binding fingerprint, and
safety posture before starting work.

## Enforced Latch boundary

- The outer/global KB is disconnected. The DB and MCP runtime are pinned to
  `<client-root>/.latch-vault/kb`.
- Latch hooks and MCP processes started by plain `claude` or `codex` under an
  initialized root cannot fall back to the outer KB. Latch refuses the request
  with an explicit instruction to use `latch-vault`; the host may continue
  without Latch according to its own hook-failure behavior.
- Latch root logs and maintenance state use
  `<client-root>/.latch-vault/home`.
- temporary files use `<client-root>/.latch-vault/tmp`;
- the launcher, path resolver, MCP proxy, daemon discovery, and MCP connection
  prelude validate the same canonical root, local KB, binding id, and marker
  fingerprint;
- the daemon validates that identity and the connection's `project_cwd` again
  before every request. A copied/tampered marker, nested vault, cross-root
  request, inherited environment outside the root, or custom path override is
  rejected before a tool call;
- seed, transcript compaction, outer import, legacy MCP fallback, and Git
  snapshot/push paths are disabled by policy in vaulted processes;
- every Latch-owned Claude, Codex, or Cursor model subprocess is replaced by a
  local fail-closed blocker because Latch cannot verify its account identity;
- the native sqlite-vec extension is disabled in favor of the existing
  brute-force cosine fallback, keeping vaulted DB execution deterministic;
- a missing or malformed binding still leaves the state directory as a
  tripwire. Widened POSIX permissions, symlinked writable directories, changed
  static links, missing assets, or an active global Latch kill switch also
  block launch;
- the state directory is mode `0700`, the binding is mode `0600`, and ordinary
  Git clones receive a local `.git/info/exclude` entry.

The configured MCP/hooks continue to execute the installed engine code. The
client-local Latch home contains a private, validated copy of only `schema.sql`
and a validated link to the local embedding-model assets. It deliberately does
not expose seed, compact, install, update, or other standalone CLI scripts.
There is no live read-only connection to the outer KB and no synchronization
path back to it.

## Deliberate limits

This is not a VM, container, network sandbox, or provider-account control. The
host Claude Code/Codex account, provider handling, host transcript storage,
shell tools, Git operations, and any software outside Latch remain governed by
the client's policy. If the NDA requires those surfaces to be isolated too, use
a client-controlled account and development environment.

The binding id and fingerprint are deterministic routing integrity values, not
secret credentials. This mode prevents accidental Latch cross-contamination;
it is not a defense against malicious code already running as the same OS user.

V1 supports Claude Code CLI and Codex CLI only. Desktop/IDE use, live reads from
the outer KB, cross-vault import/export, merging, automatic cleanup, and
deployment are intentionally out of scope until consultant acceptance testing
is explicitly approved.
