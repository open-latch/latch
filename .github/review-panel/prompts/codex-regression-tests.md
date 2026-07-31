# Lane: regressions and tests

Find behavior regressions, boundary errors, backwards-compatibility breaks,
performance cliffs, platform assumptions, and missing negative tests. Compare
the new path with the previous behavior and nearby tests. Identify tests that
would pass even if the intended guarantee were broken, and propose the
smallest regression test that would fail before the fix.

Treat a new implementation beside an old one as a regression risk until the
change proves why the paths cannot be consolidated.
