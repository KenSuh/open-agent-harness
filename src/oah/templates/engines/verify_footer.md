<!-- OAH_VERIFY_FOOTER -->
## Verify what changed
- Between review rounds, re-run only the checks whose inputs your change touched, plus the fast
  structural checks. For every other gate, note in the ledger that its earlier evidence still holds
  and that the diff did not touch its inputs.
- Run the heavy checks (full suites on every platform, cold starts, CI) once, on the final commit
  the review converged on.
- Never mark a gate met without its evidence, and never edit evidence you did not produce.
