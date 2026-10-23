"""The Grants tab's table, shared by the schema, database and table views.

One row per grantee rather than one per `SHOW GRANTS` row. The question
this tab answers is "what may this role do here?", and with one row per
privilege that needs a scan across several rows that are identical apart
from one cell. The rollup only restates what Snowflake returned: grantees
and privileges are Snowflake's own values, and the headers are its words.
What cannot be folded into one row -- each privilege's own `granted_by`,
`created_on` and `grant_option` -- is not dropped but moved: the panel under
the table lists the highlighted grantee's raw rows. `granted_on` and `name`
are left out entirely, since they are the same on every row of one object's
grants.

The cache is untouched by this: `show_grants` still mirrors every row as
`SHOW GRANTS` returned it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.text import Text
from tcoda.widgets import DataTable

from nivaria.format import format_timestamp_minutes
from nivaria.ui.views.base import _ScopedView
from nivaria.ui.views.tables import PanelledTable

#: The grants table, in whichever view is mounted -- shared the same way
#: `tables.LIST_TABLE` is.
GRANTS_TABLE = "detail-grants"

#: Shown first: whoever owns the object is the first thing worth knowing,
#: and it is not a privilege anyone else holds alongside.
_OWNERSHIP = "OWNERSHIP"


def compose_grants() -> PanelledTable:
    table = DataTable(id=GRANTS_TABLE, cursor_type="row")
    table.zebra_stripes = True
    # Each header tooltips the SHOW GRANTS field it shows, the way the column
    # list's do, as proof the column is Snowflake's.
    table.add_column("Grantee", key="grantee", tooltip="grantee_name")
    table.add_column("Granted To", key="granted_to", tooltip="granted_to")
    table.add_column(
        "Privileges", key="privileges", tooltip="privilege (+ = grant_option)"
    )
    return PanelledTable(table, describe_grantee)


def paint_grants(
    view: _ScopedView, panelled: PanelledTable, rows: Iterable[dict[str, Any]]
) -> None:
    """Repaint `panelled` with one row per grantee of `rows`.

    The repository already orders by (granted_to, grantee_name, privilege),
    so grouping in arrival order keeps that order.
    """
    grantees: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        # granted_to is a grantee type (ROLE, SHARE, ...) and never holds a
        # colon, so the first one splits the key unambiguously.
        key = f"{row.get('granted_to') or ''}:{row.get('grantee_name') or ''}"
        grantees.setdefault(key, []).append(row)

    table = panelled.table
    with view.keeping_cursor(table):
        table.clear()
        panelled.rows = {}
        for key, grants in grantees.items():
            grants.sort(key=_privilege_order)
            first = grants[0]
            panelled.rows[key] = {"grants": grants}
            table.add_row(
                Text(first.get("grantee_name") or ""),
                Text(first.get("granted_to") or ""),
                Text(
                    ", ".join(
                        f"{g.get('privilege') or ''}{'+' if _grant_option(g) else ''}"
                        for g in grants
                    )
                ),
                key=key,
            )
    panelled.sync()


def describe_grantee(row: dict[str, Any]) -> str | None:
    """The highlighted grantee's raw grants, one per line."""
    grants: list[dict[str, Any]] = row.get("grants") or []
    if not grants:
        return None
    width = max(len(g.get("privilege") or "") for g in grants)
    lines = []
    for g in grants:
        parts = [(g.get("privilege") or "").ljust(width)]
        if g.get("granted_by"):
            parts.append(f"granted by {g['granted_by']}")
        created = format_timestamp_minutes(g.get("created_on"))
        if created:
            parts.append(created)
        if _grant_option(g):
            parts.append("grant option")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def _privilege_order(grant: dict[str, Any]) -> tuple[bool, str]:
    privilege = grant.get("privilege") or ""
    return (privilege != _OWNERSHIP, privilege)


def _grant_option(grant: dict[str, Any]) -> bool:
    # SHOW GRANTS returns the strings "true"/"false", not a boolean.
    return str(grant.get("grant_option")).lower() == "true"
