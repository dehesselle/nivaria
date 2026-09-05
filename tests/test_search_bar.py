"""Find-as-you-type search over cached objects."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import ObjectKind, ObjectRef, objects_scope
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.widgets.search_bar import SearchBar


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, database: str | None = "MYDB") -> None:
        super().__init__()
        self._repo = repo
        self._database = database
        self.selected: list[ObjectRef] = []
        self.activity: list[bool] = []

    def compose(self) -> ComposeResult:
        yield SearchBar(self._repo, id="search")

    def on_mount(self) -> None:
        self.query_one(SearchBar).set_database(self._database)

    def on_object_selected(self, event: ObjectSelected) -> None:
        self.selected.append(event.ref)

    def on_search_bar_activity(self, event: SearchBar.Activity) -> None:
        self.activity.append(event.active)


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def put(repo: Repository, kind: ObjectKind, schema: str, *names: str, database="MYDB"):
    load(
        repo,
        objects_scope(kind, database, schema),
        [
            {
                "database_name": database,
                "schema_name": schema,
                "name": name,
                "kind": kind.value,
                "comment": "",
            }
            for name in names
        ],
    )


async def type_query(pilot, app: Harness, text: str) -> None:
    app.query_one("#search-query", Input).value = text
    for _ in range(4):
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()


def shown(app: Harness) -> list[str]:
    return [f"{ref.schema}.{ref.name}" for ref in app.query_one(SearchBar)._shown]


# ----------------------------------------------------------------- corpus


def test_the_corpus_spans_every_object_kind(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    put(repo, ObjectKind.VIEW, "SALES", "ORDERS_V")
    put(repo, ObjectKind.PIPE, "RAW", "LOAD")
    corpus = repo.search_corpus("MYDB")
    assert sorted(text for text, _ in corpus) == [
        "RAW.LOAD",
        "SALES.ORDERS",
        "SALES.ORDERS_V",
    ]


def test_the_corpus_carries_the_kind(repo) -> None:
    """A hit has to know which detail view to open."""
    put(repo, ObjectKind.PIPE, "RAW", "LOAD")
    ((_, ref),) = repo.search_corpus("MYDB")
    assert ref.kind is ObjectKind.PIPE


def test_the_corpus_is_scoped_to_one_database(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    put(repo, ObjectKind.TABLE, "SALES", "OTHER_ORDERS", database="OTHERDB")
    assert [text for text, _ in repo.search_corpus("MYDB")] == ["SALES.ORDERS"]


def test_an_unknown_database_has_an_empty_corpus(repo) -> None:
    assert repo.search_corpus("NOPE") == []


# ---------------------------------------------------------------- searching


async def test_results_appear_as_you_type(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS", "CUSTOMERS", "ORDER_ITEMS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#search-results", OptionList).display is False
        await type_query(pilot, app, "ord")
        assert app.query_one("#search-results", OptionList).display is True
        assert set(shown(app)) == {"SALES.ORDERS", "SALES.ORDER_ITEMS"}


async def test_matching_is_a_subsequence_not_a_substring(repo) -> None:
    """The shorthand this exists for: no substring search finds this."""
    put(repo, ObjectKind.TABLE, "STAGING", "STG_CUSTOMER_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stgord")
        assert shown(app) == ["STAGING.STG_CUSTOMER_ORDERS"]


async def test_the_schema_can_be_typed_too(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    put(repo, ObjectKind.TABLE, "RAW", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "salord")
        assert shown(app) == ["SALES.ORDERS"]


async def test_no_match_shows_nothing(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "zzzz")
        assert shown(app) == []


async def test_clearing_the_query_hides_the_results(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        await type_query(pilot, app, "")
        assert app.query_one("#search-results", OptionList).display is False
        assert app.activity[-1] is False


async def test_escape_clears_the_search(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        app.query_one(SearchBar).action_clear()
        await pilot.pause()
        assert shown(app) == []
        assert app.query_one("#search-query", Input).value == ""


# --------------------------------------------------------------- selection


async def test_submitting_chooses_the_top_hit(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS", "ORDER_ITEMS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "orders")
        app.query_one("#search-query", Input).post_message(
            Input.Submitted(app.query_one("#search-query", Input), "orders")
        )
        await pilot.pause()
    assert app.selected[0].name == "ORDERS"


async def test_a_chosen_result_carries_its_kind(repo) -> None:
    put(repo, ObjectKind.PIPE, "RAW", "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "load")
        app.query_one(SearchBar)._choose(0)
        await pilot.pause()
    assert app.selected[0].kind is ObjectKind.PIPE


# ------------------------------------------------------------------ corpus
# freshness


async def test_new_objects_become_searchable_after_a_cache_update(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "cust")
        assert shown(app) == []

        put(repo, ObjectKind.TABLE, "SALES", "ORDERS", "CUSTOMERS")
        app.query_one(SearchBar).cache_updated()
        await type_query(pilot, app, "custo")
        assert shown(app) == ["SALES.CUSTOMERS"]


async def test_the_corpus_is_not_rebuilt_on_every_keystroke(repo) -> None:
    """A database-wide prime lands dozens of updates a second; rebuilding on
    each would be work nobody is waiting on."""
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(SearchBar)
        calls: list[str] = []
        original = bar.repo.search_corpus

        def spy(database):
            calls.append(database)
            return original(database)

        bar.repo.search_corpus = spy
        await type_query(pilot, app, "o")
        await type_query(pilot, app, "or")
        await type_query(pilot, app, "ord")
        assert calls == ["MYDB"]


async def test_switching_database_resets_the_search(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        assert shown(app) != []
        app.query_one(SearchBar).set_database("OTHERDB")
        await pilot.pause()
        assert shown(app) == []
        assert app.query_one("#search-query", Input).value == ""


async def test_searching_without_a_database_is_empty_not_an_error(repo) -> None:
    app = Harness(repo, database=None)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        assert shown(app) == []


async def test_the_schema_prefix_does_not_outrank_the_object_name(repo) -> None:
    """End to end: typing 'stgord' in a STAGING schema must find the STG_
    table, not a same-schema pipe that happens to end in _ORDERS."""
    put(repo, ObjectKind.TABLE, "STAGING", "STG_CUSTOMER_ORDERS")
    put(repo, ObjectKind.PIPE, "STAGING", "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "stgord")
        assert shown(app)[0] == "STAGING.STG_CUSTOMER_ORDERS"
