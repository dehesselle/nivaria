# Developing nivaria

This document covers setting up, running, testing, linting and licensing
nivaria, and the conventions its code follows.

The architecture and the invariants a change must respect are in
[DESIGN.md](DESIGN.md#core-principles).

## Setup

nivaria uses Python 3.12 and is managed with **uv**.

```sh
uv sync --all-groups
```

Use the project `.venv` to run anything. Add a dependency with `uv add`.

The menu bar, settings and dialog chrome come from **tcoda**, a shared library
installed from PyPI like any other dependency. Its source is the sibling
checkout `../tcoda`; `../snakecam` and `../vc` use it too.

`snowflake-connector-python` is held below 5 on purpose. The 5.x line is a
rewrite on Snowflake's Rust core, so lifting the cap is a migration, not a
bump: it drops the `keyring` extra that `gateway/credential_cache.py` imports
and keeps its own token cache, which on Windows has no fallback for an OAuth
token too large for Credential Manager. It also ignores the `timeout` argument
of `cursor.execute()` that bounds a data preview, and renames or retires
several connect kwargs in `ConnectParams.to_connect_kwargs`.

## Running

```sh
.venv/Scripts/python -m nivaria
```

`textual-dev` is in the dev group, so `textual console` and `textual run
--dev` work for debugging the UI.

## Testing

```sh
.venv/Scripts/python -m pytest
```

Run pytest through `python -m`: the tests import `tests.fakes`, which needs the
repository root on `sys.path`.

- **No test may reach the network or import the Snowflake driver.** A fake
  gateway and a fake connection pool (`tests/fakes.py`) stand in, so the
  cache, scheduler and entire UI run offline.
- UI tests drive a real app through Textual's test pilot. pytest runs in
  asyncio auto mode, so async tests need no marker. After a display toggle,
  one `pilot.pause()` is not always enough for layout to settle; pump twice.
- `test_a_schema_change_comes_with_a_version_bump` hashes `schema.sql` and
  `search.sql`, comments included. Any edit to either needs a
  `SCHEMA_VERSION` bump in `cache/db.py` and a new digest in the test.
- `tests/data/show_column_types.json` holds live `DESC TABLE` output for 51
  types and pins the column type rendering.
- Fakes cannot catch a statement whose result grows without bound. Check
  Snowflake-facing changes against a live account at real scale; see
  [snowflake-behavior.md](snowflake-behavior.md).

## Linting and formatting

**ruff** is installed globally and runs on default rules. The only
`[tool.ruff]` setting is `required-version`, which pins the minor release
because ruff's defaults change between minors; raise it together with the pin
in `.github/workflows/build.yml` once the new minor's findings are reviewed.
After changes:

```sh
ruff format
ruff check
```

Let the formatter decide.

**markdownlint** checks the Markdown files:

```sh
uvx --from nodejs-wheel npx --yes markdownlint-cli2@0.24.0
```

It runs on Node, which `nodejs-wheel` provides, so no Node install is needed.
Its settings are in `.markdownlint-cli2.yaml`, which the VS Code markdownlint
extension reads too. CI uses `markdownlint-cli2-action`, which bundles a fixed
markdownlint-cli2; bump the version above together with that action's pin in
`.github/workflows/build.yml`.

## CI and releases

`.github/workflows/build.yml` runs on every push and pull request: ruff,
markdownlint and `reuse lint`, the tests on Linux, Windows and macOS with Python 3.12 (plus
3.14 on Linux), and a wheel build that is smoke-tested by importing it.

Pushing a `v*` tag additionally checks that the wheel's version matches the
tag, creates a draft GitHub release with the wheel and sdist, and publishes to
PyPI through trusted publishing (GitHub environment `pypi`).

## Licensing

The project is REUSE-compliant (`reuse` is installed globally). New files
start with the SPDX header that `reuse annotate` writes; files that can't
carry a comment get covered in `REUSE.toml`. `reuse lint` must pass.

## Testing against unreleased tcoda

```sh
just local-tcoda python -m pytest
just local-tcoda nivaria
```

`just local-tcoda <command>` overlays an editable `../tcoda` for that one
command and leaves `pyproject.toml` and `uv.lock` untouched. Do not add a
`[tool.uv.sources]` path entry for tcoda: the lock and tests must use the
release the published wheel will depend on. Plain `.venv` runs always use the
locked release.

Once nivaria needs a new tcoda feature, release tcoda first, then raise the
bound in `pyproject.toml` and run `uv lock --upgrade-package tcoda`.

## Versioning

The version comes from git tags via hatch-vcs and is written to
`src/nivaria/version.py` at build time (ignored by git). `nivaria --version`
prints it.

## Conventions

The layering rules (no Textual outside the UI layer, the gateway as the only
seam to Snowflake) are core principles in
[DESIGN.md](DESIGN.md#core-principles). Beyond those:

- Comments explain why the obvious alternative was rejected.
- Test names are sentences describing the behaviour they protect.

## Tools

The scripts in `tools/` import nothing from nivaria, so they can be copied to
a machine that reaches the account.

- `tools/probe_snowflake.py` checks `SHOW ... LIMIT n FROM 'name'` paging and
  lists the columns each `SHOW` really returns. Nothing is stored.

  ```sh
  python probe_snowflake.py --account ORG-ACCT --user you@corp.com \
      --authenticator externalbrowser --role YOUR_ROLE \
      --database SOME_DB --out probe.txt
  ```

- `tools/gen_test_ddl.py` writes `CREATE OR REPLACE` DDL that fills a sandbox
  with databases, schemas, tables and pipes for scale tests. Output goes to
  `tools/data/`, which git ignores.

  ```sh
  python tools/gen_test_ddl.py --seed 1 --out tools/data/renetest_ddl.sql
  ```

- `tools/trim_svg.py` shrinks a screenshot SVG from Textual to about a
  quarter of its size. It merges Rich's per-segment backgrounds and text
  runs and drops the per-line clip paths. The result looks the same.

  ```sh
  python tools/trim_svg.py shot.svg              # writes shot.min.svg
  python tools/trim_svg.py *.svg --in-place
  ```

## Adding a scope

A new scope touches ten places:

1. a `ScopeType` member;
2. a `*_scope()` constructor;
3. a case in `Scope.key`;
4. a `TableSpec`;
5. a case in `Repository.spec_for`;
6. a table in `schema.sql` and a `SCHEMA_VERSION` bump;
7. a builder in `gateway/queries.py`;
8. a method on the `SnowflakeGateway` protocol;
9. the same on `PoolGateway`;
10. a case in `jobs.fetch_page`.

A `SELECT`-backed scope also needs a row in DESIGN.md's
[second-source table](DESIGN.md#the-second-source-scopes).
