## Summary

<!-- What does this change, and why? Link the issue it resolves, for example "Fixes #123". -->

## How it was tested

<!-- Commands you ran and their results. -->

## Checklist

- [ ] The tests pass with Python 3.11 (`TMPDIR="$PWD/.tmp/test-tmp" python3.11 -m unittest discover -s tests -t .`).
- [ ] `python3 tools/privacy_scan.py --gitleaks require` prints `PRIVACY_SCAN_CLEAN`.
- [ ] No API key, token, password, cookie, account identifier, personal path or user name is in the change.
- [ ] New behavior has tests, and the change works on both macOS and Windows.
- [ ] The README or `docs/` is updated if users see or do something differently.
- [ ] Every commit has an `Agent-Engine: claude|codex|human` trailer.
