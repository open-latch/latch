---
description: Apply weekly decay + promote staging nodes that cleared the ref_count bar
---

Run the weekly maintenance pass on the per-project knowledge base under
`<KB_HOME>`. Two operations:

1. **Decay** — multiply every referenced node's `ref_count` by 0.9
   (floored at 1 once any node has been referenced at least once).
2. **Promote** — nodes in `status='staging'` with `ref_count >= 3` are
   promoted to `status='canonical'`.

This is the signal loop that makes access-frequency the source of truth for
what matters: nodes that are actually consulted keep their weight; nodes that
quietly stopped being useful fade.

Run with the Bash tool:

```bash
python "<KB_HOME>/src/maintenance.py" "$(pwd)"
```

Report back the `decayed_rows` count, `promoted_count`, and the list of
`promoted_ids` (with titles via `latch_get` if the user wants to see what got
promoted).

If anything fails, check `<KB_HOME>/maintenance.log`.
