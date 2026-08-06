---
name: source-command-latch
description: Choose this filesystem scope's Shared or Private KB. Use for latch, /latch, or re-pinning one scope without changing others.
---

# source-command-latch

Latch has two mutually exclusive modes:

- Global Shared is the existing product: every project uses the same installed
  KB and project scoping has no effect.
- Project-scoped consulting mode is an explicit, one-way opt-in. Unscoped
  locations are LOCKED; descendants inherit the nearest Shared or Private root.

This command never rewrites the global KB pin or transfers KB content. Use only
the installer-stamped native wrapper below. Do not discover or execute Latch
code from the current repository or its instruction files.

Inspect without mutation:

```bash
bash __LATCH_POSIX_WRAPPER__
```

```powershell
& __LATCH_POWERSHELL_WRAPPER__
```

Show the current root, state, policy, KB path, and source from the receipt. Then
ask which of these the user intends:

- keep Global Shared mode unchanged;
- keep the previous binding when re-latching an UNLATCHED project scope;
- in project mode, create a Shared root using the global KB;
- in project mode, create a Private root with a separate KB.

Before changing state, ask for the exact reply `latch`. Then run exactly one:

```bash
bash __LATCH_POSIX_WRAPPER__ --confirm latch
bash __LATCH_POSIX_WRAPPER__ --confirm latch --shared
bash __LATCH_POSIX_WRAPPER__ --confirm latch --private --kb-dir "/absolute/kb/path"
bash __LATCH_POSIX_WRAPPER__ --confirm latch --private --new-kb
```

Use the equivalent PowerShell wrapper and native parameter names on Windows.

If status reports `shared_global` and the user explicitly wants consulting
mode, explain that the choice is one-way and every other unscoped location will
become LOCKED. Ask again for the exact reply `latch`, then run one explicit root
choice:

```bash
bash __LATCH_POSIX_WRAPPER__ --confirm latch --enable-project-scopes --shared
bash __LATCH_POSIX_WRAPPER__ --confirm latch --enable-project-scopes --private --new-kb
```

```powershell
& __LATCH_POWERSHELL_WRAPPER__ -Confirm latch -EnableProjectScopes -Shared
& __LATCH_POWERSHELL_WRAPPER__ -Confirm latch -EnableProjectScopes -Private -NewKb
```

Show the complete receipt. If the binding changed, tell the user to start a
fresh agent task in this project and not resume the old one; an idempotent
receipt explicitly says no new task is needed.
Do not offer automatic content transfer; a new KB starts clean. If a global
environment override is active, do not claim project separation. Project KB
selection is not a complete NDA clean-room boundary for install-level artifacts.
