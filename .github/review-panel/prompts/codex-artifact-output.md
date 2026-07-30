# Lane: user-facing artifact and output review

Review the isolated evidence packet in `.review-panel-artifacts` alongside the
code diff. The packet contains changed user-facing files, a focused patch, and
the output or failure receipts from applicable deterministic simulation
recipes. Judge what a user would actually read or experience: confusing UX,
hidden overclaims, stale copy, broken formatting, missing receipts, misleading
errors, and mismatches between behavior and product promises.

This lane is intentionally separate from project-decision review. Do not infer
that an artifact is good merely because the code follows an internal
architecture. If no recipe could simulate an affected surface, record that
specific limitation in coverage_gaps.
