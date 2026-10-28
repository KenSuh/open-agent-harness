# Keys: where they live

`oah` itself needs no key. Claude Code and Codex sign in on their own, and the optional features that use a
key stay off without one. This page says where each key lives, what is off without it, and the one rule that
never bends.

[한국어](ko/keys.md)

## The hard rule

No API key, token, password, cookie, OAuth secret or account identifier ever goes into a project file, a
commit, an issue, a log or a screenshot. That includes `.oah/config.toml`, `.mcp.json`, `AGENTS.md` and test
fixtures.

- Keys are read only at runtime, from an environment variable or the OS keychain.
- Templates and docs use placeholder names such as `${TYPESAFE_API_KEY}`, never values.
- `oah` never asks you to paste a key into a file, and never writes one, prints one or puts one in a brief, a run
  record or an error message.

## Which keys exist

| Key | Used by | Without it |
|---|---|---|
| none | `oah` itself: leases, packets, gates, impact map, wiki, review, landing | everything works |
| your Claude Code sign-in | Claude Code. `oah` checks it with `claude auth status` and never reads it. | `oah call claude` cannot run |
| your Codex sign-in | Codex CLI. `oah` checks it with `codex login status` and never reads it. | `oah review` and `oah fix` cannot run |
| `TYPESAFE_API_KEY` | the optional TypeSafe Jev re-ranker | `rerank=unused(no key)`: the fixed rules decide alone |
| `CONTEXT7_API_KEY`, `OPENAI_API_KEY` | MCP servers from the toolkit's templates in `mcp/`, read by Claude Code or Codex when they start the server | that server does not start, or runs without the optional key; see [Toolkit](toolkit.md) |

## Claude Code and Codex use their own sign-ins

Run each tool's own sign-in in your terminal. `oah init` and `oah doctor` tell you when one is missing.

- **Claude Code:** `claude auth login`.
- **Codex with a ChatGPT plan:** `codex login`.
- **Codex with an API key:** Codex reads the key from standard input, so it never appears on the command line:

  ```sh
  printenv OPENAI_API_KEY | codex login --with-api-key
  ```

  On Windows PowerShell: `$env:OPENAI_API_KEY | codex login --with-api-key`.

Both tools keep the credential in their own configuration folder, outside your project. `oah` only runs their
status commands (`claude auth status`, `codex login status`) and reads the answer "signed in" or not.

## Where `oah` looks for a key

`oah` has exactly one lookup for keys, in `oah.secrets`. For a key named `<NAME>` it tries, in order:

1. **The environment variable `<NAME>`**, when it is set and not empty.
2. **The OS keychain**, under the service `oah` and the account `<NAME>`:
   - macOS: the login keychain, through the built-in `security` tool.
   - Windows: Credential Manager, a generic credential with the target `oah:<NAME>`.
3. **Nothing.** The feature stays off and reports `unused(no key)`.

### Store a key in the environment

macOS (zsh), in `~/.zshrc`, outside any project:

```sh
export TYPESAFE_API_KEY="<your key>"
```

Windows, for your user, then open a new terminal:

```powershell
setx TYPESAFE_API_KEY "<your key>"
```

### Store a key in the OS keychain

The keychain keeps the key out of your shell profile. Both commands prompt for the value, so it never lands in
your shell history.

macOS:

```sh
security add-generic-password -s oah -a TYPESAFE_API_KEY -w
```

Windows (Command Prompt or PowerShell):

```powershell
cmdkey /generic:oah:TYPESAFE_API_KEY /user:oah /pass
```

## A key alone never turns a feature on

The TypeSafe Jev re-ranker runs only when **you enabled it and the key is present**. A key in your environment
does not enable it. Guided `oah init` asks once, says what it sends and that TypeSafe may charge for requests,
and records your answer in `.oah/config.toml`:

```toml
[rerank]
enabled = true
provider = "typesafe"
```

Every packet plan, brief, run record and impact result then says `rerank=used` or `rerank=unused(<reason>)`:

| Result | Meaning |
|---|---|
| `rerank=used` | The re-ranker ran and re-ordered the candidates. It never adds or drops one. |
| `rerank=unused(not-enabled)` | `[rerank] enabled` is not `true`. This is the default. |
| `rerank=unused(no key)` | It is enabled, but the lookup above found no key. |
| `rerank=unused(disabled-by-brief)` | The command ran with `--no-rerank`, or the packet turned it off. |

To use another variable name, set `key_name` under `[rerank.typesafe]`.

## What keeps keys out of git

- `oah init` adds `.env`, `.env.*`, `*.pem`, `*.key`, `.oah/secrets*` and `.claude/settings.local.json` to
  `.gitignore`.
- The opt-in `pre-push` hook (`oah hooks install pre-push`) runs gitleaks over exactly the commits you push and
  refuses with `OAH_PUSH_SECRETS` on a finding.
- `oah land --check` scans the commits that would land and refuses without gitleaks (`OAH_LAND_SCAN_REQUIRED`).
- The CI starter (`oah ci init`) adds a gitleaks job to every push and pull request. See
  [CI starter](ci-starter.md).

## If a key leaks

1. **Rotate it first,** at the provider that issued it. A key that reached a commit, a push or a log is
   exposed, even if you delete it a minute later.
2. Then remove it from the files and commits, and push the cleaned history.
3. Put the new key in your environment or keychain as described above.
