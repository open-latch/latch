---
description: Preview one ruled-out decision, then write only that exact approved candidate
---

Latch operation id: latch-pm prepare

Read latch first. Ask for one concrete approach the user already ruled out and
why; sharpen at most once. Construct one staging decision with the reason in
the body and the current workstream link when known.

Call the read-only `latch_pm_preview` MCP tool with the complete structured
candidate: `kind`, `title`, `body`, `status`, normalized `links`, and
`workstream_id` when known. Treat its structured tool result as the approval
card. It performs no write and returns the digest enforced by the hooks. Do not
substitute a prose preview.

Ask the user to accept, edit, or skip. On edit, call `latch_pm_preview` again
with the revised candidate. On accept, ask the user to reply exactly
`/latch-pm apply`. Only on that later turn call `latch_insert` once with the
exact previewed fields. Do not add artifacts or a session override. Any changed
load-bearing field is denied; JSON key and link ordering are normalized.

After the write, report the staging node id and invite the user to trigger the
rejected path in a fresh conversation with `latch_gate` so the saved reason is
found before edits. Keep the proof framed as decision continuity, not generic
memory.
