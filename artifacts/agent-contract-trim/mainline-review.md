# Compact agent contract mainline review

This local integration replays the compact managed contract on public main
`c65ff621d0a8ff37cbe2056f962c15cd233b9c45`. The final contract commit is
`f0fc2a747017fc27ea6fbf628c4e18975c0ee033`. The proof packet was captured
from source commit `4ad0d51fb12734778148343db6d54d98a837b69e`; its direct proof child is
`9b139028c41db3b63462bd7c60f8745dcbf92032`. Later contract-only corrections
do not change the gate/runtime manifest covered by that proof packet. The proof
does not validate the later managed contract text; contract, sync, wiring, and
artifact checks cover that surface.

The replay preserves the newer Cursor MCP catalog recovery wording and the
current-main Cursor hook identity parser. There is no candidate diff from
public main in the current-prompt hook implementation or its focused tests.

## Local verification

- 59 focused contract, sync, wiring, public guard, and Cursor lifecycle tests
  passed.
- The complete dependency-backed suite passed: 1,077 tests, with 2 skipped, in
  108.86 seconds.
- The proof packet's gate/runtime manifest is current and public-safe; its scope
  excludes the later managed contract text.
- Public-release hygiene, whitespace, and changed-shell syntax checks passed.
- Claude and Codex managed blocks are within 100 lines and 650 words.
- The Cursor rule is 46 lines and 350 words; Cursor's combined always-loaded
  surface is 141 lines and 989 words.

A fresh independent Latch/parity review caught three omissions in the first
trim: standing-directive capture offers, completed sequence-plan promotion, and
Cursor's exact `latch_pm_preview` to one `latch_insert` boundary. All three were
restored compactly and added to the obligation/scenario corpus before the final
review.

The final current-main review also caught a shortened Cursor IDE recovery step.
The rule now requires enabling the workspace server, confirming tools are
reported without assuming a fixed count, and only then starting a fresh Agent
chat; the obligation/scenario corpus covers this wording.

## Cursor live findings

A two-run payload probe used Cursor Agent CLI `2026.07.09-a3815c0`, once with
`--force` and once without it. `sessionStart` fired in both. Tool hooks fired
when tools ran, but `beforeSubmitPrompt` did not fire in either run. Therefore
the CLI had no prompt hash to bind to a gate receipt and correctly failed
closed on mutation. This is a current host limitation shared with public main,
not a regression introduced by the compact contract.

The targeted PM replay also exposed an invalid evaluation assumption. The
installed workflow previews on one turn, asks the user to reply exactly
`/latch-pm apply`, and writes on that later turn. A one-turn test that requires
both preview and insert is not a valid product test.

The later deep evaluation must:

1. Exercise current-prompt receipt success in Cursor IDE, while retaining a
   separate CLI fail-closed compatibility case.
2. Test PM preview and apply across two turns in one resumed Cursor session,
   including exact-match success, changed-argument denial, and replay denial.
3. Treat a missing expected mutation in a blocker scenario as a hard functional
   failure.
4. Separate compact-contract behavior from host-hook availability and from
   deterministic engine enforcement.

PR 6 had already been updated to the prior verified head. This refresh is
intended to update it again only after current main advanced and the new exact
head passed verification.
