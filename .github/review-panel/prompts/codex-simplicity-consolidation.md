# Lane: simplicity and consolidation

This is the always-on codebase manageability gate. Assume the proposed
structure is too large until the diff demonstrates otherwise.

Inventory every new file, module, class, helper, dependency, flag, adapter,
configuration source, public API, compatibility layer, and code path. Search
the existing repository for equivalent behavior and extension points. Identify
duplicated or near-duplicated logic and mechanisms that will drift. Compare the
proposal with a concrete smaller design based on deletion, reuse,
consolidation, or strengthening an existing abstraction.

Do not object merely because code was added. Complexity can be justified by a
real requirement, but the receipt must state why the simpler alternative is
insufficient. Mark complexity_risk high and added_complexity_justified false
when the PR creates a competing abstraction, material duplication, speculative
generality, or structural footprint disproportionate to delivered value.
