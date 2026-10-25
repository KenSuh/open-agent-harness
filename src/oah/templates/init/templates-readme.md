# oah templates

Copies of the templates oah writes from, written by `oah init` so you can read the format:

- `PACKET.md`: the task packet that `oah packet new <task>` writes to `.oah/tasks/<task>/PACKET.md`.
- `GATES.md`: the gate ledger that `oah gates new <task>` writes to `.oah/tasks/<task>/GATES.md`.

In this oah version those two commands still use their built-in copies, so editing the files here does
not change what they write yet. Wiki note templates live in `{{WIKI_DIR}}/templates/`, where
`oah wiki new` and Obsidian's Templates plugin read them.
