# Security policy

## Supported versions

`oah` has not had a tagged release yet. Security fixes go to the `main` branch. Once releases start, the
latest release and `main` receive fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the problem, the affected command or file, and the steps to reproduce it.

Do not open a public issue, pull request or discussion for a vulnerability.

Never include a real API key, token, password, cookie, OAuth secret or account identifier in a report,
even one you have already revoked. Use a placeholder such as `${OPENAI_API_KEY}` or `sk-REDACTED`.
Remove personal paths and user names from logs before you attach them.

## What to expect

- A maintainer aims to acknowledge the report within 7 days.
- We confirm the problem, prepare a fix and agree on a disclosure date with you.
- With your consent, we credit you in the release notes or the advisory.

## Scope

In scope:

- The `oah` command-line tool in `src/oah/` and its installers.
- Templates and scripts this repository ships, including the CI starter.
- A path through which `oah` could leak a credential into a file, record, log, printed output or CI log.

Out of scope:

- Vulnerabilities in Claude Code, Codex CLI, Orca or other tools that `oah` launches. Report those to their
  maintainers.
- Problems that need an attacker who already controls your user account or your repository's write access.

The project's hard rule on secrets is described in the [README](README.md#security).
