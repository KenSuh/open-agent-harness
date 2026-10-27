# Contributing to oah

Thank you for helping. `oah` is a Python 3.11+ standard-library CLI for macOS and native Windows. This guide
covers how to report problems, propose changes and send a pull request.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Questions, bugs and ideas

- **Questions** go to the repository's **Discussions** tab.
- **Bugs** go to an issue with the bug report form.
- **Ideas and feature requests** go to an issue with the feature request form. For a large change, open the
  issue first and agree on the approach before you write code.
- **Security problems** never go to a public issue. Follow the [security policy](SECURITY.md).

## The hard rule on secrets

No API key, token, password, cookie, OAuth secret or account identifier may enter this repository, its
history, releases, issues or CI logs. This applies to issues, logs and screenshots too.

- Use placeholders such as `${OPENAI_API_KEY}` in examples and templates.
- Remove personal paths and user names from logs before you paste them.
- Keys are read only at runtime, from environment variables or the OS keychain.

## Development setup

You need Python 3.11 or newer and git. `oah` uses only the standard library, so no packages are needed to run
the tests. Point `TMPDIR` at the git-ignored `.tmp/` folder so every test write stays inside the checkout:

```sh
mkdir -p .tmp/test-tmp
TMPDIR="$PWD/.tmp/test-tmp" python3.11 -m unittest discover -s tests -t .
```

If you have a newer Python as well, run the suite with it too. CI runs the tests on macOS and Windows for every
pull request to `main`.

Before every push, run the privacy scan with gitleaks required. It must print `PRIVACY_SCAN_CLEAN`:

```sh
python3 tools/privacy_scan.py --gitleaks require
```

## Making a change

- Keep a change focused on one problem. Do not reformat or rename code you are not changing.
- Add only standard-library imports. A new runtime dependency needs an issue and agreement first.
- Code must work on macOS and native Windows: paths, line endings, file locking, signals and process launch
  differ between them.
- Add or update tests for every behavior change. A test must fail when the property it names is broken.
- Update the README or the files in `docs/` when you change what a user sees or does.

## Commits

Write commit messages in the imperative mood with a short subject, for example
`fix(lease): refuse a renewal after the lease expired`.

Every commit records which engine wrote it with an `Agent-Engine` trailer:

```text
Agent-Engine: human
```

Use `claude` or `codex` when that agent wrote the change.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make the change, run the tests and the privacy scan.
3. Open a pull request against `main` and fill in the template.
4. Address review comments with new commits; a maintainer lands the change once review and CI pass.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
