# nivaria

A Textual TUI for browsing and searching Snowflake objects.

## Documentation

- Read `docs/DESIGN.md` before architectural changes and respect its core
  principles; several are not obvious from the code and a change can silently
  break them.
- Usage, keys and settings: `docs/MANUAL.md`. Runtime behaviour, limits and
  files: `docs/OPERATIONS.md`.
- What each screen and trigger sends to Snowflake: `docs/data-retrieval.md`. Check
  it before changing what is fetched, and when.
- Live Snowflake findings: `docs/snowflake-behavior.md`. Timings:
  `docs/measurements.md`.

@docs/DEVELOPMENT.md

## Rules

- The README stays short. A change that affects design, usage or behaviour
  updates the matching document in the same change. A change to what is fetched,
  or when, updates `docs/data-retrieval.md`.
- `docs/DESIGN.md` is kept current and carries no project history.
- Commit only when asked.
