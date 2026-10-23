# MCP server templates

Launch templates for the MCP servers in the toolkit (see `docs/toolkit.md`), one per server and engine:

| Server | Claude Code | Codex | Key it reads | Upstream |
|---|---|---|---|---|
| context7 | `claude/context7.json` | `codex/context7.toml` | `CONTEXT7_API_KEY` (optional) | <https://github.com/upstash/context7> (MIT) |
| korean-law | `claude/korean-law.json` | `codex/korean-law.toml` | `LAW_OC` | <https://github.com/chrisryugj/korean-law-mcp> (MIT) |
| mcp-openai | `claude/mcp-openai.json` | `codex/mcp-openai.toml` | `OPENAI_API_KEY` | <https://github.com/mzxrai/mcp-openai> (MIT) |

Each template pins the npm version the toolkit was checked with. To move to a newer version, change the
`@<version>` suffix yourself.

## Using a template

- **Claude Code:** merge the `mcpServers` entry into your project's `.mcp.json`, or add it to your user
  configuration. Claude Code replaces `${NAME}` with the environment variable `NAME` when it starts the
  server; `${NAME:-}` means the key is optional.
- **Codex:** copy the `[mcp_servers.<name>]` table into `~/.codex/config.toml`, or into a trusted
  project's `.codex/config.toml`. `env_vars` lists the variables Codex passes through from your
  environment, so the file never holds a value.
- **Windows:** Claude Code's documentation says local servers started with `npx` need
  `"command": "cmd", "args": ["/c", "npx", ...]` on native Windows. If Codex cannot start `npx`, see its
  MCP documentation for the Windows form.

## Where your keys live

Keys never go into project files, these templates, or anything you commit. Keep them in one of these:

- **Environment variables** set in your shell profile or your user environment, outside the project.
  - macOS (zsh): add `export LAW_OC=...` to `~/.zshrc`.
  - Windows: set the variable for your user in System Settings, or with `setx LAW_OC "..."` in a
    terminal you then reopen.
- **The OS keychain** (macOS Keychain, Windows Credential Manager), read at runtime by the one lookup oah
  provides for keys. A feature whose key is missing stays off and reports `unused(no key)`.

If a key ever lands in a file by mistake, rotate it with its provider first, then remove it.
