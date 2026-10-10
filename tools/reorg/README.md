# Reorganization porting recipe

`apply_reorg.py` deterministically ports a pre-package branch from flat
`src/*.py` modules to the `src/latch/` package. It preserves basenames, rewrites
only imports from the known module map, installs literal-path entrypoint guards,
and applies occurrence-counted path patches. It is idempotent after a successful
run.

Use it only in a throwaway worktree. Merge the current target lane into the
branch first; do not rebase. Do not run moved `src/` files directly, and do not
touch or rotate the pinned runtime.

## Approved compatibility boundaries

This reorganization intentionally adds no `[project.scripts]` console entry
points. The existing `bin/` wrappers remain the canonical command surface; the
new package metadata supports imports, editable installs, and bundled resources
without publishing additional commands.

The pure move also intentionally leaves no flat `src/mcp_daemon.py`
compatibility launcher. After an in-place update of a checkout-pointing install,
run the supported manual doctor, installer, or `latch update` repair path and
start a fresh task if an active pre-package MCP proxy cannot recover its owner.
The retained proxy protocol still reports `fresh_task_required` after repair.
Pinned-runtime installs remain on their prior code until a deliberate pin
rotation.

Create an isolated tool environment so LibCST does not become a runtime
dependency:

```bash
python3 -m venv /tmp/latch-reorg-tool
/tmp/latch-reorg-tool/bin/python -m pip install -r tools/reorg/requirements.txt
```

Apply the transformation, then run its static and collection gates with a
Python interpreter that has the repository's test dependencies:

```bash
/tmp/latch-reorg-tool/bin/python tools/reorg/apply_reorg.py
/tmp/latch-reorg-tool/bin/python tools/reorg/apply_reorg.py --check \
  --pytest-python /path/to/project-test-python
```

The default node-ID baseline is the recorded `main` baseline. When porting a
different lane, pass that lane's separately recorded sorted baseline with
`--baseline-node-ids`; never weaken the exact collection check to a count.

After the codemod, resolve only genuine import-header residue from the lane
merge. Review every remaining hunk against the modularization spec, run the full
pytest suite, and verify the release hygiene check. Proof recapture and Git
history shape are separate release steps; this tool performs neither.
