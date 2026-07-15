# Compact agent contract mainline review

This local integration replays the compact managed contract on public main
`51b36412e182623978b1f141fb8719182dddd833`. The final contract commit is
`b237a5269285cb9634fc5d6cf413af68596dfa41`. The proof packet was captured
from source commit `e6234d691360cb6de0f5f523a1d615282a7b7462`; its direct proof child is
`e6a9cfc637441c4e982a50575be1e04e2952a753`. Later contract-only corrections
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
  102.42 seconds.
- The proof packet's gate/runtime manifest is current and public-safe; its scope
  excludes the later managed contract text.
- Public-release hygiene, whitespace, and changed-shell syntax checks passed.
- Claude and Codex managed blocks are within 100 lines and 650 words.
- The Cursor rule is within 50 lines and 350 words; Cursor's combined
  always-loaded surface is within 150 lines and 1,000 words.

A fresh independent Latch/parity review caught three omissions in the first
trim: standing-directive capture offers, completed sequence-plan promotion, and
Cursor's exact `latch_pm_preview` to one `latch_insert` boundary. All three were
restored compactly and added to the obligation/scenario corpus before the final
review.

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

No remote branch, pull request, comment, merge, or push was created or changed
during this local evidence capture. This public-native stack is intended to
replace the obsolete head of existing PR 6 after exact-head verification.
