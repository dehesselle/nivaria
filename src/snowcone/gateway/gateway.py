"""The only module that speaks Snowflake.

Methods return plain `list[dict]` keyed exactly as Snowflake names the result
columns -- lowercase for SHOW (`name`, `database_name`, `schema_name`), upper
for INFORMATION_SCHEMA (`TABLE_NAME`, `ORDINAL_POSITION`). Nothing is renamed
on the way in, so the cache tables downstream stay a faithful mirror and there
is no private vocabulary to learn.

`SnowflakeGateway` is a Protocol so the entire stack above it can be driven by
a fake in tests, with no driver import and no network.
"""

from __future__ import annotations

from typing import Any, Protocol

from snowcone.gateway import queries
from snowcone.gateway.pool import ConnectionPool
from snowcone.models import ObjectKind

Row = dict[str, Any]


class SnowflakeGateway(Protocol):
    def show_databases(
        self, *, after: str | None = None, limit: int = 1000
    ) -> list[Row]: ...

    def show_schemas(
        self, database: str, *, after: str | None = None, limit: int = 1000
    ) -> list[Row]: ...

    def show_objects(
        self,
        kind: ObjectKind,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[Row]: ...

    def show_objects_in_database(
        self,
        kind: ObjectKind,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[Row]: ...

    def show_columns(self, database: str, schema: str, table: str) -> list[Row]: ...


class PoolGateway:
    """A gateway that borrows a pool connection per call.

    One call == one page == one connection checkout, so no single query can
    monopolise a connection while a user is waiting to expand a node.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _fetch(
        self, statement: str, params: tuple[Any, ...] | None = None
    ) -> list[Row]:
        with self._pool.acquire() as connection:
            cursor = connection.cursor()
            try:
                if params:
                    cursor.execute(statement, params)
                else:
                    cursor.execute(statement)
                columns = [description[0] for description in cursor.description]
                return [
                    dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
                ]
            finally:
                cursor.close()

    def show_databases(
        self, *, after: str | None = None, limit: int = 1000
    ) -> list[Row]:
        return self._fetch(queries.show_databases(after=after, limit=limit))

    def show_schemas(
        self, database: str, *, after: str | None = None, limit: int = 1000
    ) -> list[Row]:
        return self._fetch(queries.show_schemas(database, after=after, limit=limit))

    def show_objects(
        self,
        kind: ObjectKind,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[Row]:
        return self._fetch(
            queries.show_objects(kind, database, schema, after=after, limit=limit)
        )

    def show_objects_in_database(
        self,
        kind: ObjectKind,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[Row]:
        return self._fetch(
            queries.show_objects_in_database(kind, database, after=after, limit=limit)
        )

    def show_columns(self, database: str, schema: str, table: str) -> list[Row]:
        rows = self._fetch(queries.show_columns(database, schema, table))
        # Mapped here rather than in the cache, so the mirror only ever sees
        # the INFORMATION_SCHEMA shape no matter which statement produced it.
        return queries.map_show_columns(rows)
