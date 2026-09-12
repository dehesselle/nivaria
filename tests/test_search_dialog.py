"""Find-as-you-type search over cached objects, as a modal dialog."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Input, OptionList

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectRef,
    ObjectType,
    SchemaRef,
    columns_scope,
    objects_scope,
    schemas_scope,
)
from snowcone.ui.screens.search import SearchDialog


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    """Pushes the dialog on mount, the same way MainScreen.action_search
    does, and records whatever it is dismissed with."""

    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, databases: Sequence[str] = ("MYDB",)) -> None:
        super().__init__()
        self._repo = repo
        self._databases = databases
        self.selected: list[ObjectRef | SchemaRef | None] = []

    def on_mount(self) -> None:
        self.push_screen(
            SearchDialog(self._repo, self._databases), self.selected.append
        )


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def put(
    repo: Repository, object_type: ObjectType, schema: str, *names: str, database="MYDB"
):
    load(
        repo,
        objects_scope(object_type, database, schema),
        [
            {
                "database_name": database,
                "schema_name": schema,
                "name": name,
                "kind": object_type.value,
                "comment": "",
            }
            for name in names
        ],
    )


def put_columns(
    repo: Repository, schema: str, table: str, *names: str, database="MYDB"
) -> None:
    load(
        repo,
        columns_scope(database, schema, table),
        [
            {
                "TABLE_CATALOG": database,
                "TABLE_SCHEMA": schema,
                "TABLE_NAME": table,
                "COLUMN_NAME": name,
                "ORDINAL_POSITION": index + 1,
                "DATA_TYPE": "TEXT",
                "IS_NULLABLE": "YES",
                "COLUMN_DEFAULT": None,
                "CHARACTER_MAXIMUM_LENGTH": None,
                "NUMERIC_PRECISION": None,
                "NUMERIC_SCALE": None,
                "COMMENT": "",
            }
            for index, name in enumerate(names)
        ],
    )


def put_schemas(repo: Repository, *names: str, database="MYDB") -> None:
    load(
        repo,
        schemas_scope(database),
        [{"database_name": database, "name": name, "comment": ""} for name in names],
    )


async def type_query(pilot, app: Harness, text: str) -> None:
    app.screen.query_one("#search-query", Input).value = text
    for _ in range(4):
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()


def shown(app: Harness) -> list[str]:
    return [f"{ref.schema}.{ref.name}" for ref in app.screen._shown]


def shown_labels(app: Harness) -> list[str]:
    """Like shown(), but also handles a SchemaRef hit, which has no schema
    of its own to qualify it with."""
    return [
        ref.name if isinstance(ref, SchemaRef) else f"{ref.schema}.{ref.name}"
        for ref in app.screen._shown
    ]


def option_text(app: Harness, index: int) -> str:
    results = app.screen.query_one("#search-results", OptionList)
    return results.get_option_at_index(index).prompt.plain


# ----------------------------------------------------------------- corpus


def test_the_corpus_spans_every_object_kind(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put(repo, ObjectType.VIEW, "SALES", "ORDERS_V")
    put(repo, ObjectType.PIPE, "RAW", "LOAD")
    corpus = repo.search_corpus(["MYDB"])
    assert sorted(text for text, _ in corpus) == [
        "RAW.LOAD",
        "SALES.ORDERS",
        "SALES.ORDERS_V",
    ]


def test_the_corpus_carries_the_kind(repo) -> None:
    """A hit has to know which detail view to open."""
    put(repo, ObjectType.PIPE, "RAW", "LOAD")
    ((_, ref),) = repo.search_corpus(["MYDB"])
    assert ref.type is ObjectType.PIPE


def test_the_corpus_is_scoped_to_the_given_databases(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put(repo, ObjectType.TABLE, "SALES", "OTHER_ORDERS", database="OTHERDB")
    put(repo, ObjectType.TABLE, "SALES", "THIRD_ORDERS", database="THIRDDB")
    assert [text for text, _ in repo.search_corpus(["MYDB"])] == ["SALES.ORDERS"]
    assert sorted(text for text, _ in repo.search_corpus(["MYDB", "OTHERDB"])) == [
        "SALES.ORDERS",
        "SALES.OTHER_ORDERS",
    ]


def test_an_empty_database_list_has_an_empty_corpus(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    assert repo.search_corpus([]) == []


def test_an_unknown_database_has_an_empty_corpus(repo) -> None:
    assert repo.search_corpus(["NOPE"]) == []


# ------------------------------------------------------------ column corpus


def test_the_column_corpus_pairs_a_column_with_its_owning_object(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put_columns(repo, "SALES", "ORDERS", "ID", "CUSTOMER_ID")
    corpus = repo.search_column_corpus(["MYDB"])
    assert sorted(column for _, column in corpus) == ["CUSTOMER_ID", "ID"]
    ((ref, _),) = [pair for pair in corpus if pair[1] == "ID"]
    assert ref == ObjectRef(
        type=ObjectType.TABLE, database="MYDB", schema="SALES", name="ORDERS"
    )


def test_a_view_can_own_columns_too(repo) -> None:
    put(repo, ObjectType.VIEW, "SALES", "ORDERS_V")
    put_columns(repo, "SALES", "ORDERS_V", "ID")
    ((ref, column),) = repo.search_column_corpus(["MYDB"])
    assert ref.type is ObjectType.VIEW
    assert column == "ID"


def test_a_column_with_no_known_owning_object_is_excluded(repo) -> None:
    """A stale leftover column row from a since-dropped table must not
    surface, so the corpus is joined back to search_object rather than read
    off information_schema_columns alone."""
    put_columns(repo, "SALES", "GHOST", "ID")
    assert repo.search_column_corpus(["MYDB"]) == []


def test_the_column_corpus_is_scoped_to_the_given_databases(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put_columns(repo, "SALES", "ORDERS", "ID")
    put(repo, ObjectType.TABLE, "SALES", "ORDERS", database="OTHERDB")
    put_columns(repo, "SALES", "ORDERS", "OTHER_ID", database="OTHERDB")
    assert [column for _, column in repo.search_column_corpus(["MYDB"])] == ["ID"]
    assert sorted(
        column for _, column in repo.search_column_corpus(["MYDB", "OTHERDB"])
    ) == ["ID", "OTHER_ID"]


def test_an_unknown_database_has_an_empty_column_corpus(repo) -> None:
    assert repo.search_column_corpus(["NOPE"]) == []


# ------------------------------------------------------------ schema corpus


def test_the_schema_corpus_lists_every_cached_schema(repo) -> None:
    put_schemas(repo, "SALES", "RAW")
    assert sorted(s.name for s in repo.search_schema_corpus(["MYDB"])) == [
        "RAW",
        "SALES",
    ]


def test_the_schema_corpus_carries_the_database(repo) -> None:
    put_schemas(repo, "SALES")
    (schema,) = repo.search_schema_corpus(["MYDB"])
    assert schema.database == "MYDB"
    assert schema.name == "SALES"


def test_the_schema_corpus_is_scoped_to_the_given_databases(repo) -> None:
    put_schemas(repo, "SALES")
    put_schemas(repo, "OTHER", database="OTHERDB")
    assert [s.name for s in repo.search_schema_corpus(["MYDB"])] == ["SALES"]
    assert sorted(s.name for s in repo.search_schema_corpus(["MYDB", "OTHERDB"])) == [
        "OTHER",
        "SALES",
    ]


def test_an_unknown_database_has_an_empty_schema_corpus(repo) -> None:
    assert repo.search_schema_corpus(["NOPE"]) == []


# ---------------------------------------------------------------- opening


async def test_the_query_box_is_focused_on_open(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#search-query", Input).has_focus


# ---------------------------------------------------------------- searching


async def test_results_appear_as_you_type(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS", "CUSTOMERS", "ORDER_ITEMS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        assert set(shown(app)) == {"SALES.ORDERS", "SALES.ORDER_ITEMS"}


async def test_matching_is_a_subsequence_not_a_substring(repo) -> None:
    """The shorthand this exists for: no substring search finds this."""
    put(repo, ObjectType.TABLE, "STAGING", "STG_CUSTOMER_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stgord")
        assert shown(app) == ["STAGING.STG_CUSTOMER_ORDERS"]


async def test_the_schema_can_be_typed_too(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put(repo, ObjectType.TABLE, "RAW", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "salord")
        assert shown(app) == ["SALES.ORDERS"]


async def test_no_match_shows_nothing(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "zzzz")
        assert shown(app) == []


async def test_clearing_the_query_empties_the_results(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        await type_query(pilot, app, "")
        assert shown(app) == []


async def test_the_schema_prefix_does_not_outrank_the_object_name(repo) -> None:
    """End to end: typing 'stgord' in a STAGING schema must find the STG_
    table, not a same-schema pipe that happens to end in _ORDERS."""
    put(repo, ObjectType.TABLE, "STAGING", "STG_CUSTOMER_ORDERS")
    put(repo, ObjectType.PIPE, "STAGING", "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stgord")
        assert shown(app)[0] == "STAGING.STG_CUSTOMER_ORDERS"


async def test_search_spans_every_selected_database(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "MYDB_ORDERS")
    put(repo, ObjectType.TABLE, "SALES", "OTHERDB_ORDERS", database="OTHERDB")
    app = Harness(repo, databases=("MYDB", "OTHERDB"))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "orders")
        assert set(shown(app)) == {"SALES.MYDB_ORDERS", "SALES.OTHERDB_ORDERS"}


async def test_the_option_text_is_prefixed_with_the_owning_database(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "orders")
        assert option_text(app, 0).startswith("MYDB.SALES.ORDERS")


async def test_a_schema_hit_shows_its_database_prefix_too(repo) -> None:
    put_schemas(repo, "STAGING")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stag")
        assert option_text(app, 0).startswith("MYDB.STAGING")


# --------------------------------------------------- column search: disabled


async def test_column_search_is_disabled_for_now(repo) -> None:
    """Cross-database column search is not ready for prime time yet -- the
    call that would load the column corpus is commented out in
    SearchDialog.on_mount (see DESIGN.md "Known gaps"), so a query that only
    matches via a column now surfaces nothing at all."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put_columns(repo, "SALES", "ORDERS", "CUSTOMER_ID")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "custid")
        assert shown(app) == []


async def test_a_query_shorter_than_the_gate_does_not_search_columns(repo) -> None:
    """A short, generic fragment scoring against the whole column corpus is
    the expensive, low-relevance case MIN_COLUMN_QUERY_LENGTH exists to skip
    -- see search.py. Column search is disabled entirely right now anyway
    (see test_column_search_is_disabled_for_now), so this stays empty either
    way, but the gate itself is still exercised once the corpus is back."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put_columns(repo, "SALES", "ORDERS", "ID")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "id")  # 2 chars, below the gate of 3
        assert shown(app) == []


async def test_a_hit_on_the_objects_own_name_still_works(repo) -> None:
    """The object corpus is unaffected by column search being disabled."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    put_columns(repo, "SALES", "ORDERS", "CUSTOMER_ID")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "orders")
        assert shown(app) == ["SALES.ORDERS"]
        assert "CUSTOMER_ID" not in option_text(app, 0)


async def test_a_name_match_is_unaffected_by_a_same_object_column(repo) -> None:
    """A name match ranks the object regardless of whether a column of the
    same object also happens to share letters with the query -- moot for
    ranking purposes while columns are disabled, but the object-name path
    itself must still work."""
    put(repo, ObjectType.TABLE, "SALES", "ORD")
    put_columns(repo, "SALES", "ORD", "ORD_STATUS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        assert shown(app) == ["SALES.ORD"]
        assert "ORD_STATUS" not in option_text(app, 0)


async def test_a_schema_name_surfaces_as_a_hit(repo) -> None:
    put_schemas(repo, "STAGING")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stag")
        assert shown_labels(app) == ["STAGING"]


async def test_a_schema_hit_is_labelled_as_a_schema(repo) -> None:
    put_schemas(repo, "STAGING")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stag")
        assert "schema" in option_text(app, 0)


async def test_choosing_a_schema_hit_dismisses_with_a_schemaref(repo) -> None:
    put_schemas(repo, "STAGING")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stag")
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected[0] == SchemaRef(database="MYDB", name="STAGING")


async def test_a_schema_and_an_object_can_both_appear_for_the_same_query(repo) -> None:
    put(repo, ObjectType.TABLE, "STAGING", "STG_ORDERS")
    put_schemas(repo, "STAGING")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "staging")
        assert set(shown_labels(app)) == {"STAGING", "STAGING.STG_ORDERS"}


async def test_searching_without_a_database_is_empty_not_an_error(repo) -> None:
    app = Harness(repo, databases=())
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        assert shown(app) == []


# --------------------------------------------------------------- selection


async def test_enter_chooses_the_top_hit_and_closes_the_dialog(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS", "ORDER_ITEMS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "orders")
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected[0].name == "ORDERS"


async def test_a_chosen_result_carries_its_kind(repo) -> None:
    put(repo, ObjectType.PIPE, "RAW", "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "load")
        app.screen._choose(0)
        await pilot.pause()
    assert app.selected[0].type is ObjectType.PIPE


async def test_escape_cancels_without_choosing(repo) -> None:
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        await pilot.press("escape")
        await pilot.pause()
    assert app.selected == [None]


# ------------------------------------------------------------------ corpus
# freshness


async def test_the_corpus_is_built_once_not_on_every_keystroke(repo) -> None:
    """A database prime lands dozens of scope updates a second; rebuilding on
    each keystroke would be work nobody is waiting on -- and see search.py for
    why there is no live update to react to anyway."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    calls: list[list[str]] = []
    original = repo.search_corpus

    def spy(databases):
        calls.append(list(databases))
        return original(databases)

    repo.search_corpus = spy
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "o")
        await type_query(pilot, app, "or")
        await type_query(pilot, app, "ord")
    assert calls == [["MYDB"]]


async def test_objects_added_after_opening_are_invisible_until_the_dialog_reopens(
    repo,
) -> None:
    """The corpus is a snapshot taken when the dialog opens; see search.py for
    why a cache update reaching it while it is open is not on the table."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        # Loading is kicked off in on_mount but finishes on its own schedule
        # (see search.py) -- awaited explicitly here so the mutation below is
        # deterministically *after* the snapshot, not racing it. Column
        # loading is disabled (self._columns_loaded stays None), so only the
        # object and schema loads are awaited.
        await app.screen._objects_loaded
        await app.screen._schemas_loaded
        put(repo, ObjectType.TABLE, "SALES", "ORDERS", "CUSTOMERS")
        await type_query(pilot, app, "cust")
        assert shown(app) == []


# ------------------------------------------------------- progressive loading


def slow_search_corpus(repo: Repository, release: threading.Event) -> None:
    """Make the corpus load block until the test releases it, so typing
    during that window is deterministic instead of racing a fast fixture."""
    original = repo.search_corpus

    def slow(databases):
        release.wait(timeout=2)
        return original(databases)

    repo.search_corpus = slow


async def test_typing_before_the_corpus_loads_shows_nothing_yet(repo) -> None:
    """A fast typist must not wait on the corpus load -- see search.py."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    release = threading.Event()
    slow_search_corpus(repo, release)
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        # The load is still blocked, so there is nothing to have matched yet
        # -- the point is that this returned at all rather than hanging.
        assert shown(app) == []
        release.set()


async def test_the_list_corrects_itself_once_a_slow_load_lands(repo) -> None:
    """The result the user actually asked for is not lost, only delayed."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    release = threading.Event()
    slow_search_corpus(repo, release)
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        release.set()
        await app.screen._objects_loaded
        await app.screen._schemas_loaded
        await pilot.pause()
        assert shown(app) == ["SALES.ORDERS"]


async def test_enter_waits_for_a_still_loading_corpus(repo) -> None:
    """Enter commits to a choice and closes the dialog, so unlike a plain
    keystroke it must not act on a still-partial corpus."""
    put(repo, ObjectType.TABLE, "SALES", "ORDERS")
    release = threading.Event()
    slow_search_corpus(repo, release)
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#search-query", Input).value = "orders"
        await pilot.pause()

        enter = asyncio.create_task(pilot.press("enter"))
        await asyncio.sleep(0.05)
        assert not enter.done(), "Enter must not resolve before the corpus does"
        release.set()
        await enter
        await pilot.pause()
    assert app.selected[0].name == "ORDERS"
