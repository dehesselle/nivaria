"""Shared harness and cache-seeding helpers for the detail-view tests."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult

import snowcone.ui.app as app_module
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectRef,
    ObjectType,
    column_tag_references_scope,
    columns_scope,
    grants_scope,
    materialized_view_stats_scope,
    objects_scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
    table_stats_scope,
    tag_references_scope,
    task_activity_history_scope,
    task_activity_scope,
    task_history_scope,
    view_stats_scope,
)
from snowcone.ui.messages import ObjectSelected, ScopeNeeded
from snowcone.ui.widgets.detail_pane import DetailPane


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self._repo = repo
        self.needed: list[str] = []
        self.selected: list[ObjectRef] = []

    def compose(self) -> ComposeResult:
        yield DetailPane(self._repo, id="detail")

    def on_scope_needed(self, event: ScopeNeeded) -> None:
        self.needed.append(event.scope.key)

    def on_object_selected(self, event: ObjectSelected) -> None:
        self.selected.append(event.ref)


def load(repo: Repository, scope, rows, *, truncated: bool = False) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation, truncated=truncated)


def put_table(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "kind": "TABLE",
        "comment": "",
        "rows": 1234,
        "bytes": 4096,
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", name)


def put_columns(repo: Repository, table: str, *columns: tuple) -> None:
    """Each column is (name, data_type) or (name, data_type, extra), where
    extra is a dict of additional INFORMATION_SCHEMA fields, e.g. precision."""
    rows = []
    for position, column in enumerate(columns, start=1):
        name, data_type, *rest = column
        row = {
            "TABLE_CATALOG": "MYDB",
            "TABLE_SCHEMA": "PUBLIC",
            "TABLE_NAME": table,
            "COLUMN_NAME": name,
            "ORDINAL_POSITION": position,
            "DATA_TYPE": data_type,
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": None,
            "COMMENT": "",
        }
        if rest:
            row.update(rest[0])
        rows.append(row)
    load(repo, columns_scope("MYDB", "PUBLIC", table), rows)


def put_table_stats(repo: Repository, table: str, **extra) -> None:
    row = {
        "TABLE_CATALOG": "MYDB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": table,
        "LAST_DDL": "2026-01-01 00:00:00",
        "LAST_ALTERED": "2026-01-02 00:00:00",
    }
    row.update(extra)
    load(repo, table_stats_scope("MYDB", "PUBLIC"), [row])


def put_view(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "comment": "",
        "text": "SELECT * FROM t",
        "is_secure": "false",
        "is_materialized": "false",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectType.VIEW, "MYDB", "PUBLIC", name)


def put_view_stats(repo: Repository, view: str, **extra) -> None:
    row = {
        "TABLE_CATALOG": "MYDB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": view,
        "LAST_DDL": "2026-01-01 00:00:00",
        "LAST_ALTERED": "2026-01-02 00:00:00",
    }
    row.update(extra)
    load(repo, view_stats_scope("MYDB", "PUBLIC"), [row])


def put_materialized_view_stats(repo: Repository, view: str, **extra) -> None:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": view,
        "rows": 42,
        "bytes": 8192,
    }
    row.update(extra)
    load(repo, materialized_view_stats_scope("MYDB", "PUBLIC"), [row])


def put_pipe(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "definition": "COPY INTO t FROM @s",
        "owner": "LOADER",
        "notification_channel": "arn:sqs:...",
        "comment": "",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectType.PIPE, "MYDB", "PUBLIC", name)


def put_pipe_activity(repo: Repository, pipe: str, **extra) -> None:
    row = {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": pipe,
        "FILE_NAME": "orders_1.csv.gz",
        "FILE_SIZE": 1024,
        "ROW_COUNT": 42,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-01-02 03:04:05",
        "TABLE_CATALOG_NAME": "MYDB",
        "TABLE_SCHEMA_NAME": "PUBLIC",
        "TABLE_NAME": "ORDERS",
    }
    row.update(extra)
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_pipe_activity_history(repo: Repository, pipe: str, **extra) -> None:
    """Seeds a single pipe's ACCOUNT_USAGE backfill scope -- what PipeView's
    Load History tab actually merges, unlike the schema-wide put_pipe_activity
    above (which PipeListView reads, and which PipeView only reads for target
    resolution now)."""
    row = {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": pipe,
        "FILE_NAME": "orders_1.csv.gz",
        "FILE_SIZE": 1024,
        "ROW_COUNT": 42,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-01-02 03:04:05",
        "TABLE_CATALOG_NAME": "MYDB",
        "TABLE_SCHEMA_NAME": "PUBLIC",
        "TABLE_NAME": "ORDERS",
    }
    row.update(extra)
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", pipe)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_pipe_history(repo: Repository, pipe: str, **extra) -> None:
    row = {
        "FILE_NAME": "orders_9.csv.gz",
        "FILE_SIZE": 2048,
        "ROW_COUNT": 100,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-03-01 08:00:00",
        "PIPE_NAME": pipe,
    }
    row.update(extra)
    scope = pipe_history_scope("MYDB", "PUBLIC", pipe)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_stage(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "created_on": "2026-01-01 00:00:00",
        "url": "s3://mybucket/path/",
        "type": "External Named",
        "owner": "LOADER",
        "comment": "",
        "storage_integration": "MY_INTEGRATION",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectType.STAGE, "MYDB", "PUBLIC", name)


def put_task(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "definition": "INSERT INTO t SELECT * FROM s",
        "owner": "LOADER",
        "warehouse": "MYWH",
        "schedule": "5 MINUTE",
        "state": "started",
        "comment": "",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectType.TASK, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectType.TASK, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectType.TASK, "MYDB", "PUBLIC", name)


def put_task_activity(repo: Repository, task: str, **extra) -> None:
    row = {
        "NAME": task,
        "DATABASE_NAME": "MYDB",
        "SCHEMA_NAME": "PUBLIC",
        "QUERY_ID": "01ab-query-id",
        "SCHEDULED_TIME": "2026-01-02 03:04:05",
        "QUERY_START_TIME": "2026-01-02 03:04:06",
        "COMPLETED_TIME": "2026-01-02 03:04:10",
        "STATE": "SUCCEEDED",
    }
    row.update(extra)
    scope = task_activity_scope("MYDB", "PUBLIC")
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_task_activity_history(repo: Repository, task: str, **extra) -> None:
    """Seeds a single task's ACCOUNT_USAGE backfill scope -- what TaskView's
    Run History tab actually merges, unlike the schema-wide put_task_activity
    above (which TaskListView reads)."""
    row = {
        "NAME": task,
        "DATABASE_NAME": "MYDB",
        "SCHEMA_NAME": "PUBLIC",
        "QUERY_ID": "01ab-query-id",
        "SCHEDULED_TIME": "2026-01-02 03:04:05",
        "QUERY_START_TIME": "2026-01-02 03:04:06",
        "COMPLETED_TIME": "2026-01-02 03:04:10",
        "STATE": "SUCCEEDED",
    }
    row.update(extra)
    scope = task_activity_history_scope("MYDB", "PUBLIC", task)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_task_history(repo: Repository, task: str, **extra) -> None:
    row = {
        "NAME": task,
        "QUERY_ID": "02cd-query-id",
        "SCHEDULED_TIME": "2026-03-01 08:00:00",
        "QUERY_START_TIME": "2026-03-01 08:00:01",
        "COMPLETED_TIME": "2026-03-01 08:00:05",
        "STATE": "SUCCEEDED",
    }
    row.update(extra)
    scope = task_history_scope("MYDB", "PUBLIC", task)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_grant(repo: Repository, database: str, schema: str | None, **extra) -> None:
    row = {
        "created_on": "2026-01-01 00:00:00",
        "privilege": "USAGE",
        "granted_on": "SCHEMA" if schema is not None else "DATABASE",
        "name": f"{database}.{schema}" if schema is not None else database,
        "granted_to": "ROLE",
        "grantee_name": "ANALYST",
        "grant_option": "false",
        "granted_by": "SYSADMIN",
    }
    row.update(extra)
    scope = grants_scope(database, schema)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_tag(repo: Repository, database: str, schema: str | None, **extra) -> None:
    row = {
        "TAG_DATABASE": database,
        "TAG_SCHEMA": "TAGS",
        "TAG_NAME": "COST_CENTER",
        "TAG_VALUE": "ENG",
        "DOMAIN": "SCHEMA" if schema is not None else "DATABASE",
    }
    row.update(extra)
    scope = tag_references_scope(database, schema)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_column_tag(repo: Repository, table: str, column: str, **extra) -> None:
    """Seeds one tag on one column of `table` -- what ColumnsView's Tags
    column and detail panel read, analogous to put_tag above but scoped to a
    single table's TAG_REFERENCES_ALL_COLUMNS pass."""
    row = {
        "TAG_DATABASE": "MYDB",
        "TAG_SCHEMA": "TAGS",
        "TAG_NAME": "PII",
        "TAG_VALUE": "true",
        "COLUMN_NAME": column,
    }
    row.update(extra)
    scope = column_tag_references_scope("MYDB", "PUBLIC", table)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
