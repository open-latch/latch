# Lane: correctness and concurrency

Trace state transitions, failure paths, retries, cleanup, concurrency,
idempotency, ownership, caching, and recovery. Look for behavior that works on
the happy path but fails after interruption, partial completion, duplicate
delivery, stale state, process restart, or simultaneous callers. Check whether
tests exercise the dangerous transition rather than merely the final result.

Challenge every newly introduced mechanism: determine whether an existing path
could carry the behavior with fewer states or fewer failure modes.
