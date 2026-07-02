# Contributing to latch

Thanks for helping improve latch. The first public repo is intentionally narrow:
make the local decision-seatbelt workflow easier to install, verify, and trust.

## Contribution Scope

Good public contributions improve the single-player local core:

- Claude Code and Codex install, doctor, uninstall, and quickstart flows
- seed/report quality and rejected-path demo reliability
- `kb_gate` receipts, citation clarity, and local eval coverage
- docs that make the first-run path sharper without expanding the product story
- portability, reliability, and safety fixes

Please keep contributions focused on the local first-run workflow unless the
maintainers explicitly open a broader scope for a specific issue or PR.

## License

By submitting a contribution, you agree that your contribution is licensed under
the Apache License, Version 2.0, the same license as this repository.

## Developer Certificate of Origin

Use a signed-off commit for contributions:

```bash
git commit -s
```

The sign-off means you certify the Developer Certificate of Origin 1.1:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## AI-Assisted Contributions

You may use AI coding tools to help draft or revise a contribution, but the
human submitter remains responsible for the contribution and its license
compliance.

Do not list AI tools, model providers, or coding agents such as Claude, Codex,
OpenAI, Anthropic, or similar systems as authors, co-authors, copyright holders,
or sign-off identities. If an AI tool helped, mention that in the PR notes when
useful for review context; do not put the tool in the commit author,
`Co-authored-by`, copyright, or DCO sign-off fields.

## Pull Requests

- Keep PRs focused and small enough to review.
- Include the commands you ran, especially for installer or gate changes.
- Do not include private transcripts, local KB files, generated `AGENTS.md` /
  `CLAUDE.md` outputs, `.latchbak` backups, or machine-specific paths.
- Prefer proof-honest docs: describe what latch does now, and keep planning or
  internal-facing artifacts out of the public tree.
