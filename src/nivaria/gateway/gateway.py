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

from collections.abc import Sequence
from typing import Any, Protocol

from nivaria.gateway import queries, sql_log
from nivaria.gateway.pool import ConnectionPool
from nivaria.models import ObjectType
from nivaria.settings import ROW_CEILING, config

Row = dict[str, Any]

#: How many rows are pulled off a cursor at a time while checking the row
#: ceiling. Big enough that the check costs nothing on an ordinary result,
#: small enough that a runaway one is noticed before it is fully in memory.
_FETCH_BATCH = 5_000


class ResultTooLargeError(RuntimeError):
    """A statement returned more rows than the row ceiling allows.

    Deliberately its own type rather than a bare RuntimeError, for one
    reason: `PoolGateway._fetch` retries any failure once against a fresh
    connection, which is right for a dropped session and wrong for this --
    a statement that returned too much will return too much again, and the
    retry would just pay for it twice.
    """


#: `cursor.description` type codes whose values the driver hands over as
#: indented JSON text: VARIANT, OBJECT, ARRAY, GEOGRAPHY and GEOMETRY, as the
#: driver's own FIELD_ID_TO_NAME numbers them. Spelled out rather than
#: imported, because this module never imports the driver.
_JSON_TEXT_TYPE_CODES = frozenset({5, 9, 10, 14, 15})


def compact_json_text(text: str) -> str:
    """The driver's indented JSON text with the whitespace between tokens
    dropped, as Snowflake's own `TO_VARCHAR` spells it.

    A scan rather than `json.loads`/`json.dumps`: a round trip would turn a
    VARIANT's 30-digit number into a float, and it fails outright on the
    `undefined` an ARRAY holds for a SQL NULL, which is not JSON.
    """
    out: list[str] = []
    in_string = escaped = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif not char.isspace():
            out.append(char)
    return "".join(out)


#: Snowflake's error number for "<object> does not exist or not authorized",
#: printed as `002003 (02000)`.
_MISSING_OBJECT_ERRNO = 2003


def is_missing_object(error: BaseException) -> bool:
    """Whether a statement failed because what it names is gone.

    Read off the driver error's `errno` rather than its message: the text
    varies by object kind ("Schema ... does not exist", "Table ... does not
    exist", "Object does not exist"), the number does not. `getattr` rather
    than an isinstance check against the driver's ProgrammingError, so this
    module still never imports the driver and a fake can raise it.
    """
    return getattr(error, "errno", None) == _MISSING_OBJECT_ERRNO


#: Snowflake's error number for "SQL execution canceled", printed as
#: `000604 (57014)`. The driver has no constant for it; it matches the
#: message text itself.
_CANCELLED_ERRNO = 604


def is_cancelled(error: BaseException) -> bool:
    """Whether a statement failed because its timeout cancelled it.

    The driver's own timer thread asks Snowflake to cancel the query once
    the `timeout` passed to `execute` runs out, and the cancellation comes
    back as an ordinary error. Read off `errno` for the same reasons as
    `is_missing_object`.
    """
    return getattr(error, "errno", None) == _CANCELLED_ERRNO


def _keeps_connection(error: BaseException) -> bool:
    """Whether an error came back over a session that is still healthy.

    Such an error is neither retried nor allowed to cost its connection:
    repeating it would fail the same way, and replacing the connection
    can mean an MFA prompt.
    """
    return is_missing_object(error) or is_cancelled(error)


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
        object_type: ObjectType,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def show_objects_in_database(
        self,
        object_type: ObjectType,
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

    def show_warehouses(self, *, allow_reconnect: bool = True) -> list[Row]: ...

    def show_roles(self, *, allow_reconnect: bool = True) -> list[Row]: ...

    def table_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def view_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def materialized_view_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def show_grants_on_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def show_grants_on_database(
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def show_grants_on_object(
        self,
        object_type: ObjectType,
        database: str,
        schema: str,
        name: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def tag_references_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def tag_references_database(
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]: ...

    def tag_references_all_columns(
        self,
        database: str,
        schema: str,
        table: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def select_data_preview(
        self,
        database: str,
        schema: str,
        table: str,
        max_rows: int,
        timeout: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def pipe_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def pipe_activity_history(
        self,
        database: str,
        schema: str,
        pipe: str,
        fetch_days: int,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def pipe_history(
        self,
        target_catalog: str,
        target_schema: str,
        target_table: str,
        pipe_name: str,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def pipe_status(
        self,
        database: str,
        schema: str,
        names: Sequence[str],
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def task_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def task_activity_history(
        self,
        database: str,
        schema: str,
        task: str,
        fetch_days: int,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...

    def task_history(
        self,
        database: str,
        schema: str,
        task: str,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]: ...


def _compact_json_columns(rows: list[Row], description: Any) -> None:
    """Compact, in place, every value of a column whose type the driver
    hands over as indented JSON text.

    Only the Data tab asks for this. Its cells are one line of 40
    characters, and the driver's indentation alone would fill one.
    """
    names = [d[0] for d in description if len(d) > 1 and d[1] in _JSON_TEXT_TYPE_CODES]
    for row in rows:
        for name in names:
            if isinstance(row[name], str):
                row[name] = compact_json_text(row[name])


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
        timeout: int | None = None,
        allow_reconnect: bool = True,
        compact_json: bool = False,
    ) -> list[Row]:
        # One retry, against a freshly-acquired connection, covers what a
        # checkout-time is_closed() check cannot see: a session Snowflake
        # dropped server-side, which only surfaces once actually queried.
        # Safe to retry blindly -- every statement here is a read-only SHOW or
        # SELECT -- so a genuine (non-staleness) failure just fails
        # identically the second time, at the cost of one extra round trip on
        # an already-rare path.
        try:
            return self._fetch_once(
                statement,
                params,
                timeout=timeout,
                allow_reconnect=allow_reconnect,
                compact_json=compact_json,
            )
        except ResultTooLargeError:
            # A failure the retry above must not repeat: it is a property of
            # the statement, not of the connection.
            raise
        except Exception as error:  # see pool.acquire, same boundary
            if _keeps_connection(error):
                # Same: a dropped object stays dropped on a fresh connection,
                # and a statement that ran out its timeout would run it out
                # again.
                raise
            return self._fetch_once(
                statement,
                params,
                timeout=timeout,
                allow_reconnect=allow_reconnect,
                compact_json=compact_json,
            )

    def _fetch_once(
        self,
        statement: str,
        params: tuple[Any, ...] | None,
        *,
        timeout: int | None,
        allow_reconnect: bool,
        compact_json: bool,
    ) -> list[Row]:
        healthy: Exception | None = None
        with self._pool.acquire(allow_reconnect=allow_reconnect) as connection:
            cursor = connection.cursor()
            try:
                sql_log.log_statement(cursor, statement, params)
                # The worker thread running this cannot be interrupted, but
                # the driver can: with a timeout it starts a timer thread of
                # its own that asks Snowflake to cancel the query, and this
                # call then raises. None or 0 starts no timer.
                if params:
                    cursor.execute(statement, params, timeout=timeout)
                else:
                    cursor.execute(statement, timeout=timeout)
                columns = [description[0] for description in cursor.description]
                rows = self._collect(cursor, columns, statement)
                if compact_json:
                    _compact_json_columns(rows, cursor.description)
                return rows
            except Exception as error:
                # Anything escaping this block makes the pool discard the
                # connection and open a replacement -- right for a dead
                # session, but a "does not exist" or a cancellation came back
                # over a perfectly healthy one, and a reconnect can mean an MFA
                # prompt. So those leave the block normally and are raised
                # outside. Kept this narrow on purpose: an expired session is
                # a ProgrammingError too, and that one must still be discarded.
                if not _keeps_connection(error):
                    raise
                healthy = error
            finally:
                cursor.close()
        assert healthy is not None
        raise healthy

    @staticmethod
    def _collect(cursor: Any, columns: list[str], statement: str) -> list[Row]:
        """Read a cursor out in batches, abandoning it past the row ceiling.

        The last line of defence, and the only bound that applies to every
        statement rather than to the ones somebody remembered to cap.
        Snowflake has repeatedly returned more than nivaria asked for --
        LIMIT is a no-op for several SHOW commands, and a single pipe's
        COPY_HISTORY is bounded by nothing but its time window -- so the
        statement builders' own caps are an expectation, not a guarantee.

        Be clear about what this does buy: it bounds what nivaria
        *materialises* and hands the pool connection back early, rather than
        spending minutes building dicts nobody asked for. It does not stop
        Snowflake from having produced the result. Raising rather than
        truncating is deliberate: the ceiling is meant to be unreachable, so
        reaching it is a bug worth seeing, and `fail_pass` keeps the rows
        already on screen while it is investigated.
        """
        ceiling = config.get(ROW_CEILING)
        rows: list[Row] = []
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                return rows
            rows.extend(dict(zip(columns, row, strict=True)) for row in batch)
            if 0 < ceiling < len(rows):
                raise ResultTooLargeError(
                    f"statement returned more than {ceiling} rows "
                    f"(row ceiling): {statement[:80]}"
                )

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
        object_type: ObjectType,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_objects(
                object_type, database, schema, after=after, limit=limit
            ),
            allow_reconnect=allow_reconnect,
        )

    def show_objects_in_database(
        self,
        object_type: ObjectType,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_objects_in_database(
                object_type, database, after=after, limit=limit
            ),
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

    def view_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        return self._fetch(
            queries.view_stats(database),
            (schema, "VIEW"),
            allow_reconnect=allow_reconnect,
        )

    def materialized_view_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        # Unpaged, like table_stats -- a schema's materialized-view count is
        # bounded, same reasoning show_columns/show_grants_on_schema use.
        return self._fetch(
            queries.show_materialized_views(
                database, schema, limit=queries.MAX_PAGE_SIZE
            ),
            allow_reconnect=allow_reconnect,
        )

    def show_grants_on_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        return self._fetch(
            queries.show_grants_on_schema(database, schema),
            allow_reconnect=allow_reconnect,
        )

    def show_grants_on_database(
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        return self._fetch(
            queries.show_grants_on_database(database),
            allow_reconnect=allow_reconnect,
        )

    def show_grants_on_object(
        self,
        object_type: ObjectType,
        database: str,
        schema: str,
        name: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.show_grants_on_object(object_type, database, schema, name),
            allow_reconnect=allow_reconnect,
        )

    def tag_references_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        return self._fetch(
            queries.tag_references(database),
            (f"{database}.{schema}", "SCHEMA"),
            allow_reconnect=allow_reconnect,
        )

    def tag_references_database(
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        return self._fetch(
            queries.tag_references(database),
            (database, "DATABASE"),
            allow_reconnect=allow_reconnect,
        )

    def tag_references_all_columns(
        self,
        database: str,
        schema: str,
        table: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.tag_references_all_columns(database),
            (f"{database}.{schema}.{table}",),
            allow_reconnect=allow_reconnect,
        )

    def select_data_preview(
        self,
        database: str,
        schema: str,
        table: str,
        max_rows: int,
        timeout: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        # The only statement with a timeout. LIMIT bounds what a view's
        # preview returns, not the work behind it: one that aggregates or
        # sorts computes all of its input first.
        return self._fetch(
            queries.data_preview(database, schema, table, max_rows),
            timeout=timeout,
            allow_reconnect=allow_reconnect,
            compact_json=True,
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

    def pipe_activity_history(
        self,
        database: str,
        schema: str,
        pipe: str,
        fetch_days: int,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.pipe_activity_history(fetch_days, max_rows),
            (database, schema, pipe),
            allow_reconnect=allow_reconnect,
        )

    def pipe_history(
        self,
        target_catalog: str,
        target_schema: str,
        target_table: str,
        pipe_name: str,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.pipe_history(target_catalog, target_schema, target_table, max_rows),
            (pipe_name,),
            allow_reconnect=allow_reconnect,
        )

    def pipe_status(
        self,
        database: str,
        schema: str,
        names: Sequence[str],
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.pipe_status(database, schema, names),
            allow_reconnect=allow_reconnect,
        )

    def task_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.task_activity(fetch_days),
            (database, schema),
            allow_reconnect=allow_reconnect,
        )

    def task_activity_history(
        self,
        database: str,
        schema: str,
        task: str,
        fetch_days: int,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.task_activity_history(fetch_days, max_rows),
            (database, schema, task),
            allow_reconnect=allow_reconnect,
        )

    def task_history(
        self,
        database: str,
        schema: str,
        task: str,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        return self._fetch(
            queries.task_history(database, schema, task, max_rows),
            (),
            allow_reconnect=allow_reconnect,
        )
