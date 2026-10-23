# Gates: {task}

Replace these examples with outcomes that prove this task is done, then run `oah gates lint {task}`.
Each CHECK line must run the same in cmd.exe and sh: call `python`, `node` or `oah check`, never
`grep` or pipes. `python` must start Python 3 in your terminal; if it does not, use the command that
does. Format: https://github.com/Leonxlnx/unlazy/blob/16671491f6679ad9378f52604d3bc2415b4120c7/references/gates.md

- [ ] G1: the README explains how to use the change
  CHECK: oah check contains README.md "## Usage"
  EXPECT: OAH_CHECK_OK
  EVIDENCE: pending

- [ ] G2: no TODO markers remain in the changed module
  CHECK: oah check absent src/app.py TODO
  EXPECT: OAH_CHECK_OK
  EVIDENCE: pending

- [ ] G3: the test suite passes
  CHECK: python -m unittest discover -s tests
  EXPECT: /^OK/m
  EVIDENCE: pending
