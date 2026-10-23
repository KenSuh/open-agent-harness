# P3a Config Loader and Command Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two seams that the next seven lanes build on in parallel:
- a config loader each lane calls with its own defaults, so no lane edits a shared defaults table;
- command discovery, so a lane adds `oah <command>` by adding a module and never edits `cli.py`.

**Architecture:**
- **Config:** `oah.config.load_config(root)` reads `<root>/.oah/config.toml` once with `tomllib`. Each
  lane asks for its own table with `Config.section(name, defaults)`, which validates the table against
  those defaults and returns a fresh merged dict.
- **Discovery:** every `oah/<name>_cli.py` is a command module. `cli.py` finds them with
  `pkgutil.iter_modules(oah.__path__)`, lets each register its subcommands, and dispatches on
  `args.command`.
- **Package data:** `src/oah/templates/<lane>/…` ships in the wheel.

**Tech Stack:** Python 3.11+ stdlib (`tomllib`, `pkgutil`, `importlib`, `argparse`, `zipfile`,
`unittest`).

**Spec:** `docs/PLAN.md` (5.1, 5.2, 5.3, 5.5, 7, 8)

## Global Constraints

- Everything from the P1 and P2 plans' Global Constraints still applies: stdlib only, never
  `shell=True`, UTF-8 everywhere, a Korean one-line header on new `.py` files, test writes inside the
  checkout.
- **`config.py` holds no other module's defaults.** Each lane passes its own to `section()`.
- **A missing config file is not an error.** It reads as an empty file.
- **Config errors** are `ConfigError` with `code`:
  - `OAH_CONFIG_INVALID`: unreadable or non-UTF-8 file, invalid TOML, a section that is not a table,
    or a value whose type differs from its default;
  - `OAH_CONFIG_UNKNOWN_KEY`: a key that the section's defaults do not define.
  - The message names the file, the dotted key (`stages.review`) and what to change.
- **Type rules:**
  - The value's type must be the default's type.
  - `int` is accepted for a `float` default and returned as a `float`. `bool` never counts as `int`.
  - An array default (list or tuple) accepts a TOML array; elements are not checked.
  - A `None` default accepts any value (TOML has no null, so `None` means "unset").
  - An empty `{}` default table is open-ended: any keys and values pass unchecked.
- **Command modules** expose `register(subparsers)`, `COMMANDS: tuple[str, ...]` and `run(args) -> int`.
  A missing attribute, a command claimed twice, or a module whose `register` adds commands other than
  its `COMMANDS` is a programming error: `build_parser`/`main` raise `RuntimeError` naming the module.
- `--version`, the Python version check, `configure_stdio()` and the no-command help (print, exit 0)
  stay as they are.

## Interfaces

- `config.CONFIG_RELATIVE_PATH = PurePosixPath(".oah/config.toml")`.
- `config.ConfigError(code: str, message: str)`, with `.code`; `str()` is `"<code>: <message>"`.
- `config.Config(path: Path | None, raw: Mapping[str, Any])`, frozen. `raw` is read-only all the way
  down (tables are `MappingProxyType`, arrays are tuples).
  - `Config.section(name: str, defaults: Mapping[str, Any]) -> dict`.
  - `Config.sections() -> tuple[str, ...]`: top-level table names, in file order.
- `config.load_config(root: Path) -> Config`.
- `cli.command_modules() -> list[ModuleType]`: the `*_cli` modules, sorted by module name.
- `cli.build_parser() -> ArgumentParser` and `cli.main(argv) -> int`, as today.
- `doctor_cli.COMMANDS = ("doctor",)`, `doctor_cli.register`, `doctor_cli.run`.

## Review Focus

1. **A lane's defaults leak through mutation.** Mutating a `section()` result changes neither the
   defaults nor later results. → Task 1 `test_result_is_isolated_from_defaults_and_cache`.
2. **`true` sneaks in as `1`.** → Task 1 `test_bool_is_never_an_int`.
3. **Two lanes claim the same command name.** → Task 2 `test_duplicate_command_raises`.
4. **Wheel misses template files.** → Task 4 `test_wheel_ships_templates`.

---

### Task 1: Config loader

**Files:**
- Create: `src/oah/config.py`, `tests/test_config.py`

- [ ] **Step 1: Failing tests:**
  - `test_missing_file_gives_empty_config`: `path is None`, `raw == {}`, `sections() == ()`.
  - `test_missing_section_returns_copy_of_defaults`.
  - `test_deep_merge_over_defaults`: nested tables merge key by key.
  - `test_unknown_key_raises` (top level and nested; message names file and dotted key).
  - `test_empty_defaults_table_is_open_ended`.
  - `test_type_mismatch_raises` (str for int; scalar for table; table for scalar).
  - `test_int_accepted_for_float`.
  - `test_bool_is_never_an_int`.
  - `test_none_default_accepts_any_value` and `test_array_default_accepts_array`.
  - `test_crlf_file` and `test_utf8_bom_file`.
  - `test_invalid_toml_raises_config_invalid` and `test_non_utf8_raises_config_invalid`.
  - `test_non_table_section_raises`.
  - `test_result_is_isolated_from_defaults_and_cache`.
  - `test_raw_is_read_only`.
  - `test_sections_lists_top_level_tables`.
- [ ] **Step 2: Run.** Expected: ImportError.
- [ ] **Step 3: Implement.** **Step 4: Run.** Expected: OK. **Step 5: Commit.**

### Task 2: Command discovery

**Files:**
- Modify: `src/oah/cli.py`, `tests/test_cli.py`

- [ ] **Step 1: Failing tests:**
  - `test_real_command_modules_are_discovered`: `doctor_cli` and `lease_cli`, sorted, every command
    claimed once.
  - `test_extra_module_is_discovered_and_dispatched`: a temporary `zz_extra_cli.py` on `oah.__path__`.
  - `test_duplicate_command_raises`.
  - `test_missing_attribute_raises`.
  - `test_unclaimed_registered_command_raises`.
  - `test_help_groups_commands_by_module`.
- [ ] **Step 2: Run.** Expected: failures. **Step 3: Implement.** **Step 4: Run.** **Step 5: Commit.**

### Task 3: `doctor` as a command module

**Files:**
- Create: `src/oah/doctor_cli.py`, `tests/test_doctor_cli.py`

- [ ] **Step 1: Failing tests:**
  - `test_doctor_cli_convention`: `COMMANDS == ("doctor",)`, `register` adds `doctor` with `--json`.
  - `test_run_passes_json_flag`: `run` calls `doctor_main(json_output=…)` and returns its code.
- [ ] **Step 2–5:** run, implement, run (existing `test_cli_under_cp949_io_encoding` stays green),
  commit. `doctor.py` is unchanged.

### Task 4: Package data

**Files:**
- Create: `src/oah/templates/README.md`, `tests/test_packaging.py`
- Modify: `pyproject.toml` (`[tool.setuptools.package-data] oah = ["templates/**/*"]`)

- [ ] **Step 1: Failing test:** `test_wheel_ships_templates` copies the sources to a temp folder, adds
  a nested sample `templates/sample/nested/x.txt`, builds a wheel with `pip wheel --no-deps` (no build
  isolation when the interpreter has setuptools ≥ 77), and checks that both
  `oah/templates/README.md` and the sample are in the wheel.
- [ ] **Step 2–5:** run, implement, run, commit.
