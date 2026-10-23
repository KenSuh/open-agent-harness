---
title: "{{title}}"
date: {{date}}
sources: ["https://help.obsidian.md/"]
tags: [wiki]
---
# {{title}}

This folder is the project's long-term memory, shared by people and agents. Open it as a vault
in Obsidian, or edit the files in any editor.

- Every note starts with frontmatter: `title`, `date`, `sources` and `tags`.
- Every note names at least one source: a file path with a commit, a URL, or a task packet.
- Link notes with double-bracket wikilinks, and list each note in [[index]].
- The [[log]] is newest first. `oah wiki log "<title>"` adds an entry; never edit archived logs.
- `raw/` holds imported source documents. Add files there; never edit them.
- `oah wiki lint` lists problems; it never deletes, merges or moves a note.

New notes: `oah wiki new "<title>"`, or Obsidian's template command with the `templates` folder.
