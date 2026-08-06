# Lane: user-facing artifact and output review

Review the supplied immutable diff, changed and nearby blobs, and path index
with a strict user-facing-output lens. Judge what a user would actually read or
experience: confusing UX, hidden overclaims, stale copy, broken formatting,
misleading errors, and mismatches between behavior and product promises.

This lane is intentionally separate from project-decision review. Do not infer
that an artifact is good merely because the code follows an internal
architecture. The local trust boundary never executes project code, builds, or
simulation recipes. Record any conclusion that requires rendered or runtime
output as a specific coverage gap instead of fabricating that evidence.
