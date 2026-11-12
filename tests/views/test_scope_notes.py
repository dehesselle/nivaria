# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The notes saying why a tab or a list's columns look empty.

A blank cell or an empty tab reads the same whether its query is still out,
came back with nothing, or failed -- see `tables.sync_scope_note` and
`tables.source_line`.
"""

from __future__ import annotations

from textual.widgets import Static, TabbedContent

from nivaria.models import (
    ObjectType,
    SchemaRef,
    column_tag_references_scope,
    database_roles_scope,
    grants_scope,
    materialized_view_stats_scope,
    object_grants_scope,
    objects_scope,
    pipe_activity_scope,
    pipe_status_scope,
    table_stats_scope,
    tag_references_scope,
)
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    load,
    put_pipe,
    put_table,
    put_table_stats,
    put_task,
    put_view,
)

_NO_WAREHOUSE = (
    "000606 (57P03): No active warehouse selected in the current session.\n"
    "Select an active warehouse with the 'use warehouse' command."
)


def _fail(repo, scope, message: str = _NO_WAREHOUSE) -> None:
    repo.fail_pass(scope, repo.begin_pass(scope), message)


def _note(pane, note_id: str) -> Static | None:
    note = pane.query_one(f"#{note_id}", Static)
    return note if note.display else None


def _text(pane, note_id: str) -> str | None:
    note = _note(pane, note_id)
    return str(note.render()) if note is not None else None


# ------------------------------------------------------------------ lists


async def test_a_list_says_its_second_source_columns_are_still_coming(repo) -> None:
    put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert _text(pane, "detail-source-note") == (
            "Last DDL, Last Altered: looking..."
        )


async def test_a_list_says_why_its_second_source_columns_are_blank(repo) -> None:
    """Without a warehouse those columns stay blank for good; after the
    error toast is gone, only this line says why. Snowflake's last line is
    the reason, without the error number."""
    put_table(repo, "ORDERS")
    _fail(repo, table_stats_scope("MYDB", "PUBLIC"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        note = _note(pane, "detail-source-note")
        assert note is not None
        assert str(note.render()) == (
            "Last DDL, Last Altered: could not fetch: Select an active warehouse "
            "with the 'use warehouse' command."
        )
        assert note.has_class("-error")


async def test_a_list_whose_second_source_answered_says_nothing(repo) -> None:
    put_table(repo, "ORDERS")
    put_table_stats(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert _note(pane, "detail-source-note") is None


async def test_a_refresh_of_second_source_columns_stays_quiet(repo) -> None:
    """The values on screen stay while it runs, as with every refresh."""
    put_table(repo, "ORDERS")
    put_table_stats(repo, "ORDERS")
    repo.begin_pass(table_stats_scope("MYDB", "PUBLIC"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert _note(pane, "detail-source-note") is None


async def test_a_failed_refresh_of_second_source_columns_says_how_old_they_are(
    repo,
) -> None:
    put_table(repo, "ORDERS")
    put_table_stats(repo, "ORDERS")
    _fail(repo, table_stats_scope("MYDB", "PUBLIC"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        text = _text(pane, "detail-source-note")
        assert text is not None
        assert text.startswith("Last DDL, Last Altered: as fetched ")
        assert text.endswith(
            "; the refresh failed: Select an active warehouse with the 'use "
            "warehouse' command."
        )


async def test_a_list_with_two_second_sources_has_a_line_for_each(repo) -> None:
    put_view(repo, "ORDERS_V")
    _fail(repo, materialized_view_stats_scope("MYDB", "PUBLIC"), "boom")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert _text(pane, "detail-source-note") == (
            "Last DDL, Last Altered: looking...\nRows, Size: could not fetch: boom"
        )


async def test_the_task_list_says_its_run_columns_are_still_coming(repo) -> None:
    put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.TASK, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert _text(pane, "detail-source-note") == (
            "Last Scheduled, Last Run State: looking..."
        )


async def test_the_pipe_lists_activity_tab_says_why_its_columns_are_blank(
    repo,
) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    _fail(
        repo,
        pipe_activity_scope("MYDB", "PUBLIC"),
        "SQL access control error:\nInsufficient privileges to operate on "
        "database 'SNOWFLAKE'",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"))
        await pilot.pause()
        note = pane.query_one("#tab-activity").query_one("#detail-source-note")
        assert str(note.render()) == (
            "Load activity: could not fetch: Insufficient privileges to operate "
            "on database 'SNOWFLAKE'"
        )


async def test_the_status_tab_says_how_far_a_first_status_pass_got(repo) -> None:
    """A pass reads 50 pipes per statement, so a big schema's first one
    takes a while; its row count grows with every batch written."""
    for name in ("P_A", "P_B", "P_C"):
        put_pipe(repo, name)
    status = pipe_status_scope("MYDB", "PUBLIC")
    generation = repo.begin_pass(status)
    repo.write_page(
        status,
        generation,
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": name,
                "PIPE_STATUS": '{"executionState":"RUNNING"}',
            }
            for name in ("P_A", "P_B")
        ],
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert _text(pane, "detail-status-source-note") == (
            "Execution State: reading, 2 of 3 pipes..."
        )


# ------------------------------------------------------------------- tabs


async def test_the_columns_tab_says_why_its_tags_column_is_blank(repo) -> None:
    ref = put_table(repo, "ORDERS")
    _fail(repo, column_tag_references_scope("MYDB", "PUBLIC", "ORDERS"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert _text(pane, "detail-source-note") == (
            "Tags: could not fetch: Select an active warehouse with the 'use "
            "warehouse' command."
        )


async def test_a_grants_tab_says_it_is_still_looking(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-grants"
        await pilot.pause()
        assert _text(pane, "detail-grants-note") == "Looking for grants..."


async def test_an_empty_grants_tab_says_there_are_none_the_role_can_see(
    repo,
) -> None:
    """SHOW GRANTS ON lists only what the role may see, so an empty answer
    is no claim about the object's grants as such."""
    ref = put_table(repo, "ORDERS")
    load(repo, object_grants_scope(ref), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-grants"
        await pilot.pause()
        assert _text(pane, "detail-grants-note") == (
            "No grants on this table that your role can see."
        )


async def test_the_data_tab_says_it_is_still_looking(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-data"
        await pilot.pause()
        assert _text(pane, "detail-data-note") == "Looking for rows..."


async def test_a_schemas_tabs_say_what_they_know(repo) -> None:
    load(repo, grants_scope("MYDB", "PUBLIC"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        assert _text(pane, "detail-grants-note") == (
            "No grants on this schema that your role can see."
        )
        view.query_one(TabbedContent).active = "tab-tags"
        await pilot.pause()
        assert _text(pane, "detail-tags-note") == "Looking for tags..."


async def test_a_databases_empty_tags_tab_says_there_are_none(repo) -> None:
    load(repo, tag_references_scope("MYDB", None), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_database("MYDB")
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-tags"
        await pilot.pause()
        assert _text(pane, "detail-tags-note") == "No tags on this database."


async def test_a_databases_roles_tab_says_what_it_knows(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_database("MYDB")
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-roles"
        await pilot.pause()
        assert _text(pane, "detail-roles-note") == "Looking for database roles..."

        load(repo, database_roles_scope("MYDB"), [])
        pane.cache_updated(database_roles_scope("MYDB"))
        await pilot.pause()
        assert _text(pane, "detail-roles-note") == (
            "No database roles in this database."
        )
