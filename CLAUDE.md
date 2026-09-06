# snowcone

A Textual TUI for browsing and searching Snowflake objects.

**Read `DESIGN.md` first.** It carries the architecture and the reasoning
behind it, including several constraints that are not obvious from the code and
that a change can silently break.

## Environment

- Python 3.12, managed with **uv**. Use the project `.venv` to run anything:
  `.venv/Scripts/python -m pytest`.
- `uv sync --all-groups` for dependencies, `uv add` to introduce one.
- The menu bar, settings and dialog chrome come from **`tcw`** (`../tcw`),
  a shared library consumed as an editable path dependency. Changing them
  means editing that checkout, and `../snakecam` and `../vc` use it too.
- **ruff** is installed globally and runs on **default settings** — there is
  deliberately no `[tool.ruff]` configuration. Run `ruff format` and
  `ruff check` after changes and let the formatter decide.

## Conventions

- Modules outside the UI layer do not import Textual.
- No test may reach the network or import the Snowflake driver; fakes stand in
  for the gateway and the connection pool.
- Comments explain why the obvious alternative was rejected. Test names are
  sentences describing the behaviour they protect.
- Commit only when asked.
