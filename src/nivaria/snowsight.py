# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Links into Snowsight, Snowflake's web UI.

Snowsight's object pages are hash routes under the account's base URL, e.g.
`https://app.snowflake.com/myorg/myacct/#/data/databases/DB/schemas/SC/pipe/P`.
Snowflake does not document them; they were read off Snowsight's own address
bar (see docs/snowflake-behavior.md "Snowsight links"). The base is either
the account's Snowsight URL setting or the public address built from the
organization and account name the pool read at connect
(`ConnectionPool.account_names`), so building a link never asks Snowflake.
"""

from __future__ import annotations

from urllib.parse import quote

from nivaria.gateway.pool import AccountNames
from nivaria.models import ObjectRef, ObjectType

#: The route segment Snowsight uses for each object type. Spelled out rather
#: than derived from the enum value: the two only happen to agree, and a new
#: ObjectType should fail here rather than produce a guessed link.
_SEGMENT = {
    ObjectType.TABLE: "table",
    ObjectType.VIEW: "view",
    ObjectType.PIPE: "pipe",
    ObjectType.STAGE: "stage",
    ObjectType.TASK: "task",
}


def base_url(
    names: AccountNames | None, *, privatelink: bool, override: str = ""
) -> str | None:
    """The account's Snowsight URL, ending in `/`, or None if unknown.

    `override` (the SNOWSIGHT_URL setting) wins. Without one, a PrivateLink
    account gets no link: Snowflake documents two private Snowsight hosts, a
    regionless and a regional one, and which of them resolves depends on the
    DNS the account's network admins set up. A real PrivateLink account used
    the one the regionless guess would have missed, and the call that would
    tell them apart needs ACCOUNTADMIN.
    """
    if override.strip():
        return override.strip().rstrip("/") + "/"
    if privatelink or names is None:
        return None
    org, account = names.organization.lower(), names.account.lower()
    return f"https://app.snowflake.com/{org}/{account}/"


def database_url(base: str, database: str) -> str:
    return f"{base}#/data/databases/{_part(database)}"


def schema_url(base: str, database: str, schema: str) -> str:
    return f"{database_url(base, database)}/schemas/{_part(schema)}"


def object_url(base: str, ref: ObjectRef) -> str:
    return (
        f"{schema_url(base, ref.database, ref.schema)}"
        f"/{_SEGMENT[ref.type]}/{_part(ref.name)}"
    )


def _part(name: str) -> str:
    """One path segment: the name as Snowflake stores it, percent-encoded.

    `safe=""` because a quoted identifier can hold a `/`, which would
    otherwise split the route.
    """
    return quote(name, safe="")
