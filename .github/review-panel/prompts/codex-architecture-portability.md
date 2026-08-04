# Lane: architecture and portability

Review module boundaries, ownership, dependency direction, API compatibility,
operational failure domains, and behavior across Linux, macOS, and Windows.
For agent-facing behavior, assess Claude Code, Codex, and Cursor against their
actual host contracts rather than assuming identical capabilities.

Look especially for another adapter, wrapper, configuration source, or
compatibility branch where an existing extension point should be strengthened
instead. Flag architecture that makes the next change require editing several
parallel mechanisms.
