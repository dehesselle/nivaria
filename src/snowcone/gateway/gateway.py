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
        self,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_schemas(
        self,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_objects(
        self,
        kind: ObjectKind,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_objects_in_database(
        self,
        kind: ObjectKind,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_columns(
        self,
        database: str,
        schema: str,
        table: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_columns_in_database(
        self,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_columns_in_schema(
        self,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_warehouses(self, *, allow_reconnect: bool = True) -> list[Row]: ...

    def show_roles(self, *, allow_reconnect: bool = True) -> list[Row]: ...

    def table_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def pipe_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def pipe_history(
        self,
        target_catalog: str,
        target_schema: str,
        target_table: str,
        pipe_name: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...


class PoolGateway:
    """A gateway that borrows a pool connection per call.

    One call == one page == one connection checkout, so no single query can
    monopolise a connection while a user is waiting to expand a node.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _fetch(
        self,
        statement: str,
        params: tuple[Any, ...] | None = None,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        # One retry, against a freshly-acquired connection, covers what a
        # checkout-time is_closed() check cannot see: a session Snowflake
        # dropped server-side, which only surfaces once actually queried.
        # Safe to retry blindly -- every statement here is a read-only SHOW or
        # INFORMATION_SCHEMA SELECT -- so a genuine (non-staleness) failure
        # just fails identically the second time, at the cost of one extra
        # round trip on an already-rare path.
        try:
            return self._fetch_once(statement, params, allow_reconnect=allow_reconnect)
        except Exception:  # noqa: BLE001 -- see pool.acquire, same boundary
            return self._fetch_once(statement, params, allow_reconnect=allow_reconnect)

    def _fetch_once(
        self,
        statement: str,
        params: tuple[Any, ...] | None,
        *,
        allow_reconnect: bool,
    ) -> list[Row]:
        with self._pool.acquire(allow_reconnect=allow_reconnect) as connection:
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
        self,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_databases(after=after, limit=limit),
            allow_reconnect=allow_reconnect,
        )

    def show_schemas(
        self,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_schemas(database, after=after, limit=limit),
            allow_reconnect=allow_reconnect,
        )

    def show_objects(
        self,
        kind: ObjectKind,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_objects(kind, database, schema, after=after, limit=limit),
            allow_reconnect=allow_reconnect,
        )

    def show_objects_in_database(
        self,
        kind: ObjectKind,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_objects_in_database(kind, database, after=after, limit=limit),
            allow_reconnect=allow_reconnect,
        )

    def show_columns(
        self,
        database: str,
        schema: str,
        table: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        rows = self._fetch(
            queries.show_columns(database, schema, table),
            allow_reconnect=allow_reconnect,
        )
        # Mapped here rather than in the cache, so the mirror only ever sees
        # the INFORMATION_SCHEMA shape no matter which statement produced it.
        return queries.map_show_columns(rows)

    def show_columns_in_database(
        self,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        rows = self._fetch(
            queries.show_columns_in_database(database, after=after, limit=limit),
            allow_reconnect=allow_reconnect,
        )
        return queries.map_show_columns(rows)

    def show_columns_in_schema(
        self,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        rows = self._fetch(
            queries.show_columns_in_schema(database, schema, after=after, limit=limit),
            allow_reconnect=allow_reconnect,
        )
        return queries.map_show_columns(rows)

    def show_warehouses(self, *, allow_reconnect: bool = True) -> list[Row]:
        return self._fetch(queries.show_warehouses(), allow_reconnect=allow_reconnect)

    def show_roles(self, *, allow_reconnect: bool = True) -> list[Row]:
        return self._fetch(queries.show_roles(), allow_reconnect=allow_reconnect)

    def table_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        return self._fetch(
            queries.table_stats(database),
            (schema, "BASE TABLE"),
            allow_reconnect=allow_reconnect,
        )

    def pipe_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.pipe_activity(fetch_days),
            (database, schema),
            allow_reconnect=allow_reconnect,
        )

    def pipe_history(
        self,
        target_catalog: str,
        target_schema: str,
        target_table: str,
        pipe_name: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.pipe_history(target_catalog, target_schema, target_table),
            (pipe_name,),
            allow_reconnect=allow_reconnect,
        )
