# Conformance fixtures

`v0.4.0/` holds golden fixtures recorded from the frozen Python package at tag `v0.4.0`. They show what a Rust
port must do to behave like v0.4.0, without keeping the Python tree runnable. The Rust crate `oah-conformance`
loads them; each area stays `pending` until the slice that ports it switches it on.

## Layout

```
v0.4.0/<area>/MANIFEST.json
v0.4.0/<area>/<case>/input.json      call arguments and input files
v0.4.0/<area>/<case>/expect.json
```

Areas: `config`, `rules`, `review`, `license`, `doctor`, `models`, `skills-roots`, `hashes`. Case names match
`[a-z0-9][a-z0-9-]*`.

- **Input files.** A case's input files live in `input.json`: `files` maps a relative path with `/` to the file's
  UTF-8 text (CRLF, a byte order mark and a missing final newline are kept exactly), and `bytes` maps a path to
  lowercase hex for content that is not UTF-8. A runner writes both into one folder before the call.
  Every file under `conformance/` is UTF-8 with LF line ends and no CR byte (a Rust test checks it).
  `conformance/.gitattributes` sets `* -text` so Git never converts these bytes, including on CRLF checkouts.
- **expect.json** is `{"ok": <value>}` or `{"error": {"code": "OAH_..."}}`. An `error.message` key appears only
  where the message is part of the behaviour and must be compared exactly. The config manifest's `notes`
  describe its message normalization: removing the leading error code and replacing the temporary filename.
- **JSON escapes.** v0.4.0 source ids start with `sk-`, which the repository privacy scan reads as an API key, so
  the extractor writes that `s` as `\u0073`. Any JSON parser reads the same value; compare parsed values, not
  raw text.
- **Table cases** pin a function that returns a value for many inputs: `input.json` has `calls` (row name to
  arguments) and `ok` maps each row name to its result.
- **Placeholders.** No fixture holds a machine path. A temporary folder appears as `<root>` or `<base>` followed
  by a relative path with `/`; a runner substitutes its own folder. Other placeholders are named in the area's
  manifest notes.

## MANIFEST.json

| Key | Meaning |
|---|---|
| `area`, `status` | The area and its state: `pending` until a port switches it on. |
| `oracle` | Always `v0.4.0`. |
| `functions` | The v0.4.0 functions the area pins: `module`, `name`, `file` under `src/`. |
| `mined_from` | The test files (or public source) the cases were taken from. |
| `cases` | Every case: `name`, `source` and `function`. `source` is `v0.4.0` (output of the frozen code), `design` (the 1.0 design's own table; not a v0.4.0 output) or `public` (a published vector). |
| `omitted` | Behaviour left out, with the reason (for example, it depends on the host system). |
| `deviations` | Cases whose result the 1.0 design deliberately changes, with the design section. A port is expected to differ there. |
| `notes` | How to read the area's inputs and outputs. |

## Re-running the extractor

```sh
python3.11 -I tools/conformance/extract.py           # rewrite v0.4.0/ from the frozen package
python3.11 -I tools/conformance/extract.py --check   # compare a fresh extraction with the committed tree
```

The extractor uses the Python 3.11 standard library and Git only, and imports the package from `src/`. It refuses
to run (exit 1) unless `src/oah` is the tree of tag `v0.4.0` with no uncommitted change. It runs v0.4.0 with
`HOME`, `USERPROFILE`, the XDG folders and `OAH_HOME` pointed at synthetic temporary folders. Output is
deterministic: JSON with sorted keys, two-space indent, LF and a final newline, and no clock, random value,
absolute path, user name or host value. `--check` prints `CONFORMANCE_MATCH files=<n>` and exits 0, or lists each
difference and exits 1.

## Privacy scan

Every fixture file must strictly parse as the extractor's canonical JSON: sorted keys, two-space indent,
ASCII escapes (including the source-id escape above), LF and one final newline. Duplicate keys, lone
surrogates and invalid strings fail the Rust privacy test. JSON string values and keys are scanned, and
embedded JSON documents are strictly parsed and decoded recursively. An embedded document starts with an
object, quoted string or JSON array value after whitespace and an optional BOM. The `NaN` scalar is allowed
only inside embedded input documents as a rejection-case marker. TOML section headings and the lone opening
brace used as an invalid-input marker are plain body text. Other body text permits CR, LF, tab and ANSI SGR
controls and only literal backslash, quote and regex-dot escapes; other forms are refused rather than interpreted.
There is no raw-only fallback or recovery after a rejected document or string.

Only an object-valued `bytes` field defines a hex layer: its values must be lowercase, even-length hex
strings. A UTF-16 BOM selects strict little- or big-endian decoding; other bytes use lossy UTF-8. Ordinary
strings that look like hex are not decoded. The same path, address and secret-assignment rules apply to every layer. Addresses include
punycode top-level labels and bracketed literals; dotless domains and percent-encoding are not address
layers. Secret assignments permit only exact `$NAME` or `${NAME}` references, optionally quoted, with no
bare uppercase allowance. `FORMAT.md` is plain printable ASCII Markdown with this title, these five ordered
headings, balanced code fences and a final LF; `.gitattributes` is exactly `* -text` followed by LF. Both
plain documents are scanned directly, and only these two paths are exempt from the JSON grammar.
