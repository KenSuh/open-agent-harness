# Design records

`oah design` writes these files. Each project folder holds `project.json` and one folder per design: `design.json`
(the current and accepted revision), `revisions/r<N>/` (one revision's manifest and text, never changed after it is
written) and `acceptances/a<N>.json` (one acceptance each, never changed).

Read and edit a design in its wiki page instead (`designs/` in the project wiki), then ask the AI to save the edit as
a new revision. Commit these files with the change that made them. Designs change only on the branch the project was
registered on; other branches read that branch's committed copy.
