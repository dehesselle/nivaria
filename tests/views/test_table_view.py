"""TableView: a table's column list."""

from __future__ import annotations

from tcoda.widgets import DataTable as TcodaDataTable
from textual.widgets import DataTable, Static

from snowcone.models import (
    column_tag_references_scope,
    columns_scope,
)
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    load,
    put_column_tag,
    put_columns,
    put_table,
)


def _click_header(table: DataTable, label: str) -> DataTable.HeaderSelected:
    """Build the message a real click on the `label` header posts -- the
    same event `ColumnsView.on_data_table_header_selected` bubbles up.
    Matches by prefix, not equality: the active sort column's label carries
    a trailing sort indicator ("Name ↑") that a bare click target never
    names, and no header word is a prefix of another."""
    column = next(c for c in table.ordered_columns if c.label.plain.startswith(label))
    index = next(i for i, c in enumerate(table.ordered_columns) if c is column)
    return DataTable.HeaderSelected(table, column.key, index, column.label)


async def test_the_table_view_lists_cached_columns(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("TOTAL", "FLOAT"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 1)).plain == "ID"
        assert table.get_cell_at((1, 2)).plain == "FLOAT"


async def test_the_type_column_shows_desc_tables_rendering_not_data_type(
    repo,
) -> None:
    """DATA_TYPE alone is NUMBER; the column list shows the full type, as
    rendered when the column was cached (see test_show_columns.py)."""
    ref = put_table(repo, "ORDERS")
    put_columns(
        repo,
        "ORDERS",
        ("TOTAL", "NUMBER", {"_desc_type": "NUMBER(38,0)"}),
        ("CREATED_AT", "TIMESTAMP_LTZ", {"_desc_type": "TIMESTAMP_LTZ(9)"}),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        assert table.get_cell_at((0, 2)).plain == "NUMBER(38,0)"
        assert table.get_cell_at((1, 2)).plain == "TIMESTAMP_LTZ(9)"


async def test_missing_columns_are_requested(repo) -> None:
    """The pane paints from the cache and asks for what is not there."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert "columns:MYDB.PUBLIC.ORDERS" in app.needed


async def test_cached_columns_are_not_re_requested(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    put_column_tag(repo, "ORDERS", "ID")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert app.needed == []


async def test_a_cache_update_for_the_shown_table_repaints(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 0

        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        pane.cache_updated(columns_scope("MYDB", "PUBLIC", "ORDERS"))
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 1


async def test_an_unrelated_cache_update_is_ignored(repo) -> None:
    """Views subscribe to their own scopes, not to everything."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        pane.cache_updated(columns_scope("MYDB", "PUBLIC", "SOMETHING_ELSE"))
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 0


async def test_the_pane_reports_the_scopes_it_reads(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert [s.key for s in pane.scopes()] == [
            "columns:MYDB.PUBLIC.ORDERS",
            "column_tag_references:MYDB.PUBLIC.ORDERS",
        ]


async def test_columns_default_to_declaration_order(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]


async def test_clicking_the_name_header_sorts_alphabetically_then_reverses(
    repo,
) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["ID", "TOTAL"]

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]


async def test_clicking_ordinal_after_a_name_sort_returns_declaration_order(
    repo,
) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        table.post_message(_click_header(table, "Ordinal"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]


async def test_moving_to_another_table_resets_the_sort(repo) -> None:
    """Re-pointing the reused view at a different object must not carry the
    previous table's sort choice along with it."""
    orders = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    customers = put_table(repo, "CUSTOMERS")
    put_columns(repo, "CUSTOMERS", ("NAME", "TEXT"), ("EMAIL", "TEXT"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(orders)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        table.post_message(_click_header(table, "Name"))
        await pilot.pause()

        pane.show(customers)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == [
            "NAME",
            "EMAIL",
        ]


async def test_the_tags_column_reads_blank_before_the_tags_scope_has_fetched(
    repo,
) -> None:
    """A "0" would claim more than is actually known -- see ColumnsView."""
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert table.get_cell_at((0, 5)).plain == ""


async def test_the_tags_column_reads_zero_once_confirmed_untagged(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    # A completed pass with a tag on some *other* table confirms ORDERS'
    # own scope as fetched-and-empty, the same COMPLETE/zero-rows shape
    # sync_list_note relies on elsewhere.
    put_column_tag(repo, "OTHER_TABLE", "SOME_COLUMN")
    load(repo, column_tag_references_scope("MYDB", "PUBLIC", "ORDERS"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert table.get_cell_at((0, 5)).plain == "0"


async def test_the_tags_column_counts_a_columns_tags(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("EMAIL", "TEXT"))
    put_column_tag(repo, "ORDERS", "EMAIL", TAG_NAME="PII")
    put_column_tag(repo, "ORDERS", "EMAIL", TAG_NAME="OWNER", TAG_VALUE="analytics")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert table.get_cell_at((0, 5)).plain == "0"
        assert table.get_cell_at((1, 5)).plain == "2"


async def test_clicking_the_type_header_sorts_by_formatted_type_then_reverses(
    repo,
) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(
        repo,
        "ORDERS",
        ("ID", "TEXT"),
        ("TOTAL", "NUMBER", {"_desc_type": "NUMBER(38,0)"}),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        table.post_message(_click_header(table, "Type"))
        await pilot.pause()
        # "NUMBER(38,0)" < "VARCHAR" -- ascending flips declaration order.
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]

        table.post_message(_click_header(table, "Type"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["ID", "TOTAL"]


async def test_clicking_the_tags_header_sorts_by_tag_count_then_reverses(
    repo,
) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("EMAIL", "TEXT"), ("ID", "NUMBER"))
    put_column_tag(repo, "ORDERS", "EMAIL", TAG_NAME="PII")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        table.post_message(_click_header(table, "Tags"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["ID", "EMAIL"]

        table.post_message(_click_header(table, "Tags"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["EMAIL", "ID"]


async def test_the_active_sort_columns_header_shows_a_direction_arrow(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        def label(prefix: str) -> str:
            return next(
                c.label.plain
                for c in table.ordered_columns
                if c.label.plain.startswith(prefix)
            )

        # Default sort is Ordinal, ascending; every other sortable column
        # shows the inactive indicator, not nothing, so it reads as clickable.
        assert label("Ordinal") == f"Ordinal {TcodaDataTable.SORT_ASCENDING}"
        assert label("Name") == f"Name {TcodaDataTable.SORT_INACTIVE}"
        assert label("Type") == f"Type {TcodaDataTable.SORT_INACTIVE}"
        assert label("Tags") == f"Tags {TcodaDataTable.SORT_INACTIVE}"

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert label("Name") == f"Name {TcodaDataTable.SORT_ASCENDING}"
        # Reverted, no longer active.
        assert label("Ordinal") == f"Ordinal {TcodaDataTable.SORT_INACTIVE}"

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert label("Name") == f"Name {TcodaDataTable.SORT_DESCENDING}"


async def test_a_tagged_columns_tags_show_in_the_detail_panel(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("EMAIL", "TEXT"))
    put_column_tag(repo, "ORDERS", "EMAIL", TAG_NAME="PII", TAG_VALUE="true")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        panel = pane.query_one("#detail-item", Static)

        # Row 0 is ID, untagged.
        assert not panel.display

        table.move_cursor(row=1)
        await pilot.pause()
        assert panel.display
        assert "PII = true" in str(panel.render())

        table.move_cursor(row=0)
        await pilot.pause()
        assert not panel.display


#: What hovering each header of the column list shows. Type and Tags have no
#: single Snowflake field behind them -- see `ColumnsView._add_columns`.
_HEADER_TOOLTIPS = {
    "Ordinal": "ORDINAL_POSITION",
    "Name": "COLUMN_NAME",
    "Type": None,
    "Nullable": "IS_NULLABLE",
    "Default": "COLUMN_DEFAULT",
    "Tags": None,
    "Comment": "COMMENT",
}


async def _hover_header(pilot, table: DataTable, label: str) -> None:
    # The header is the table's first line, and a column starts where the
    # ones before it end; prefix match as in `_click_header`.
    index = next(
        i
        for i, c in enumerate(table.ordered_columns)
        if c.label.plain.startswith(label)
    )
    x = sum(c.get_render_width(table) for c in table.ordered_columns[:index])
    await pilot.hover(table, offset=(x + 1, 0))
    await pilot.pause()


async def test_each_column_list_header_tooltips_its_snowflake_name(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(120, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        for label, expected in _HEADER_TOOLTIPS.items():
            await _hover_header(pilot, table, label)
            assert table.tooltip == expected, label


async def test_a_header_keeps_its_tooltip_once_sorted_on(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("TOTAL", "FLOAT"))
    app = Harness(repo)
    async with app.run_test(size=(120, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert table.sort_column == "name"
        await _hover_header(pilot, table, "Name")
        assert table.tooltip == "COLUMN_NAME"


def test_every_header_tooltip_names_a_field_of_the_cached_column_list(repo) -> None:
    """The tooltips exist to show Snowflake's own name, and the cache keeps
    Snowflake's names -- so one the cache does not have is one this app made
    up, or one left behind by a rename."""
    with repo._db.connect() as connection:
        cached = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(information_schema_columns)"
            )
        }
    named = {tooltip for tooltip in _HEADER_TOOLTIPS.values() if tooltip}
    assert named <= cached, sorted(named - cached)


async def test_showing_the_table_already_shown_keeps_its_sort(repo) -> None:
    """Pointing the pane at what it already shows -- picking the open table
    again in search, say -- is not moving to another table."""
    orders = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(orders)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        table.post_message(_click_header(table, "Name"))
        await pilot.pause()

        pane.show(orders)
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == [
            "ID",
            "TOTAL",
        ]


async def test_a_failed_fetchs_own_announcement_does_not_request_it_again(
    repo,
) -> None:
    """A scope whose first fetch failed has never been fetched, so it reads
    as stale. Re-requesting it from the repaint its own failure caused
    turned a failing statement into a retry loop; the sweep and navigating
    back are what retry it."""
    orders = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    tags = column_tag_references_scope("MYDB", "PUBLIC", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(orders)
        await pilot.pause()
        generation = repo.begin_pass(tags)
        repo.fail_pass(tags, generation, "No active warehouse selected")
        app.needed.clear()

        pane.cache_updated(tags)
        await pilot.pause()
    assert tags.key not in app.needed
