# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""SchemaView and DatabaseView: grants and tags."""

from __future__ import annotations

from textual.widgets import DataTable, Static, TabbedContent

from nivaria.models import (
    SchemaRef,
    grants_scope,
    tag_references_scope,
)
from nivaria.ui.views.schema_view import DatabaseView, SchemaView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_grant,
    put_tag,
)


async def test_showing_a_schema_mounts_the_schema_view(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert isinstance(pane.view, SchemaView)


async def test_showing_a_database_mounts_the_database_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_database("MYDB")
        await pilot.pause()
        assert isinstance(pane.view, DatabaseView)


async def test_moving_between_two_schemas_reuses_the_schema_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view_a = pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        view_b = pane.show_schema(SchemaRef(database="MYDB", name="OTHER"))
        await pilot.pause()
        assert view_a is view_b
        assert pane.view.ref.name == "OTHER"


async def test_moving_from_a_schema_to_a_database_replaces_the_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        pane.show_database("MYDB")
        await pilot.pause()
        assert isinstance(pane.view, DatabaseView)
        assert len(pane.query(SchemaView)) == 0


async def test_moving_from_a_database_to_a_schema_replaces_the_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_database("MYDB")
        await pilot.pause()
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        assert isinstance(pane.view, SchemaView)
        assert len(pane.query(DatabaseView)) == 0


async def test_missing_grants_and_tags_are_requested(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show_schema(ref)
        await pilot.pause()
    assert grants_scope("MYDB", "PUBLIC").key in app.needed
    assert tag_references_scope("MYDB", "PUBLIC").key in app.needed


async def test_cached_grants_and_tags_are_not_re_requested(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_grant(repo, "MYDB", "PUBLIC")
    put_tag(repo, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show_schema(ref)
        await pilot.pause()
    assert app.needed == []


async def test_the_grants_table_lists_every_cached_grantee(repo) -> None:
    """granted_to isn't filtered down to roles -- a DATABASE ROLE grant shows
    up right alongside a ROLE grant."""
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        granted_to="ROLE",
        grantee_name="ANALYST",
        privilege="USAGE",
    )
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        granted_to="DATABASE ROLE",
        grantee_name="APP_ROLE",
        privilege="SELECT",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        table = pane.query_one("#detail-grants", DataTable)
        assert table.row_count == 2
        granted_to = {table.get_cell_at((0, 1)).plain, table.get_cell_at((1, 1)).plain}
        assert granted_to == {"ROLE", "DATABASE ROLE"}


async def test_a_grantees_privileges_share_one_row(repo) -> None:
    """OWNERSHIP first, the rest alphabetical, `+` marking grant option."""
    put_grant(repo, "MYDB", "PUBLIC", grantee_name="ETL", privilege="USAGE")
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        grantee_name="ETL",
        privilege="CREATE TABLE",
        grant_option="true",
    )
    put_grant(repo, "MYDB", "PUBLIC", grantee_name="ETL", privilege="OWNERSHIP")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        table = pane.query_one("#detail-grants", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 0)).plain == "ETL"
        assert table.get_cell_at((0, 2)).plain == "OWNERSHIP, CREATE TABLE+, USAGE"


async def test_the_panel_lists_the_highlighted_grantees_raw_grants(repo) -> None:
    """What the grouped row cannot hold -- who granted each privilege, when,
    and with grant option -- is in the panel, not lost."""
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        grantee_name="ETL",
        privilege="USAGE",
        granted_by="SYSADMIN",
    )
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        grantee_name="ETL",
        privilege="MONITOR",
        granted_by="SECURITYADMIN",
        grant_option="true",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        panel = pane.query_one("#detail-grants-item", Static)
        assert panel.display
        lines = str(panel.render()).splitlines()
        assert lines[0].startswith("MONITOR  granted by SECURITYADMIN")
        assert lines[0].endswith("grant option")
        assert lines[1].startswith("USAGE    granted by SYSADMIN")
        assert not lines[1].endswith("grant option")


async def test_the_tags_table_lists_name_and_value_pairs(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_tag(repo, "MYDB", "PUBLIC", TAG_NAME="COST_CENTER", TAG_VALUE="ENG")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        table = pane.query_one("#detail-tags", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 0)).plain == "COST_CENTER"
        assert table.get_cell_at((0, 1)).plain == "ENG"


async def test_the_tags_table_lists_each_value_of_a_multi_value_tag(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_tag(repo, "MYDB", "PUBLIC", TAG_NAME="COST_CENTER", TAG_VALUE="ENG")
    put_tag(repo, "MYDB", "PUBLIC", TAG_NAME="COST_CENTER", TAG_VALUE="OPS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        table = pane.query_one("#detail-tags", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 1)).plain == "ENG"
        assert table.get_cell_at((1, 1)).plain == "OPS"


async def test_tags_render_blank_without_a_successful_tag_references_pass(repo) -> None:
    """The no-warehouse degrade-blank case: Grants (metadata layer) still
    renders even though Tags (a SELECT that needs a warehouse) has nothing
    cached -- same pattern as
    test_table_stats_columns_are_blank_without_a_successful_stats_pass."""
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_grant(repo, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert pane.query_one("#detail-grants", DataTable).row_count == 1
        assert pane.query_one("#detail-tags", DataTable).row_count == 0


async def test_a_cache_update_for_the_shown_schemas_grants_repaints(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert pane.query_one("#detail-grants", DataTable).row_count == 0

        put_grant(repo, "MYDB", "PUBLIC")
        pane.cache_updated(grants_scope("MYDB", "PUBLIC"))
        await pilot.pause()
        assert pane.query_one("#detail-grants", DataTable).row_count == 1


async def test_a_cache_update_for_the_shown_schemas_tags_repaints(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert pane.query_one("#detail-tags", DataTable).row_count == 0

        put_tag(repo, "MYDB", "PUBLIC")
        pane.cache_updated(tag_references_scope("MYDB", "PUBLIC"))
        await pilot.pause()
        assert pane.query_one("#detail-tags", DataTable).row_count == 1


def test_schema_view_scopes_includes_both_grants_and_tags(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    view = SchemaView(repo, ref)
    assert grants_scope("MYDB", "PUBLIC") in view.scopes()
    assert tag_references_scope("MYDB", "PUBLIC") in view.scopes()


def test_database_view_scopes_includes_both_grants_and_tags(repo) -> None:
    view = DatabaseView(repo, "MYDB")
    assert grants_scope("MYDB") in view.scopes()
    assert tag_references_scope("MYDB") in view.scopes()


async def test_the_grants_tab_is_active_by_default(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_schema(ref)
        await pilot.pause()
        assert view.query_one(TabbedContent).active == "tab-grants"


async def test_the_schema_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_schema(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-grants"
        view.action_show_tab("tags")
        assert tabs.active == "tab-tags"
        view.action_show_tab("grants")
        assert tabs.active == "tab-grants"


async def test_the_database_tab_mnemonic_actions_switch_tabs(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_database("MYDB")
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-grants"
        view.action_show_tab("tags")
        assert tabs.active == "tab-tags"


async def test_a_repaint_keeps_the_highlighted_grantee(repo) -> None:
    """A grantee row is keyed by its grantee, so the cursor stays on it
    rather than falling back to the first row."""
    put_grant(repo, "MYDB", "PUBLIC", grantee_name="A")
    put_grant(repo, "MYDB", "PUBLIC", grantee_name="B")
    put_grant(repo, "MYDB", "PUBLIC", grantee_name="C")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        table = pane.query_one("#detail-grants", DataTable)
        table.move_cursor(row=2)
        await pilot.pause()

        pane.cache_updated(grants_scope("MYDB", "PUBLIC"))
        await pilot.pause()

        assert table.cursor_row == 2
