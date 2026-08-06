The arguments passed to the runner must match exactly one of these forms:

- zero arguments
- `--pr N`
- `--pr N --post-pr`
- `--range OID...OID`
- `--range OID..OID`
- `--commit OID`

Zero arguments are allowed only to let the runner auto-detect the current
branch's pull request when the user supplies no target or unambiguously asks to
review the current PR without publication. `N` must match `[1-9][0-9]*`. Each
final `OID` must match `[0-9a-f]{40}`. Translate a bare PR number to `--pr N`.
For a user-supplied commit or range endpoint, first require each `REV` to match
`[A-Za-z0-9][A-Za-z0-9._/@{}~^+-]*`, then resolve it as a commit without an
evaluation boundary (equivalent argv: `git`, `rev-parse`, `--verify`,
`--end-of-options`, `REV^{commit}`) and pass only the resulting full OID. A
range must contain exactly one `...` or `..` separator and two nonempty
endpoints. Append `--post-pr` only to `--pr N`, and only when the user explicitly
requests publication.

Never pass user text as a shell fragment. Reject whitespace inside a target,
shell operators (`;`, `&`, `|`), redirection (`<`, `>`), command substitution
(`$(` or backticks), quotes, backslashes, leading `-`, any character outside
the `REV` grammar, or any extra flag. Do not use `eval`, `sh -c`, or an
equivalent command-string boundary. Empty target text must produce exactly zero
arguments, never a partial option. If nonempty target text is ambiguous, fails
validation, cannot be resolved to a commit, or cannot be represented by one of
the forms above, ask the user for a valid target instead of invoking the runner.
