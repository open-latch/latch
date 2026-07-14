# Compact agent contract mainline review

This local integration replays the compact managed contract on public main
`5d33047e9532eb091b46faaeac32d4d3d44f4ce0`. The source commit is
`2450c24aab35b4302c0dbb07a324a214cc538d53`; its direct proof child is
`1463ade7516237bd00b3a72dfe42ab5192780a94`.

The replay preserves the newer Cursor MCP catalog recovery wording and the
current-main Cursor hook identity parser. There is no candidate diff from
public main in the current-prompt hook implementation or its focused tests.

## Local verification

- 77 focused contract, sync, wiring, public guard, and Cursor lifecycle tests
  passed.
- The complete dependency-backed suite passed: 1,061 tests in 96.72 seconds.
- The proof packet is current and public-safe.
- Public-release hygiene, whitespace, and changed-shell syntax checks passed.
- Claude and Codex managed blocks are within 100 lines and 650 words.
- The Cursor rule is within 50 lines and 350 words; Cursor's combined
  always-loaded surface is within 150 lines and 1,000 words.

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
during this integration.
