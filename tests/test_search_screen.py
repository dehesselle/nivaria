"""Find-as-you-type search over cached objects, as a modal dialog."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Input

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import ObjectKind, ObjectRef, objects_scope
from snowcone.ui.screens.search import SearchScreen


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    """Pushes the dialog on mount, the same way MainScreen.action_search
    does, and records whatever it is dismissed with."""

    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, database: str | None = "MYDB") -> None:
        super().__init__()
        self._repo = repo
        self._database = database
        self.selected: list[ObjectRef | None] = []

    def on_mount(self) -> None:
        self.push_screen(SearchScreen(self._repo, self._database), self.selected.append)


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
    app.screen.query_one("#search-query", Input).value = text
    for _ in range(4):
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()


def shown(app: Harness) -> list[str]:
    return [f"{ref.schema}.{ref.name}" for ref in app.screen._shown]


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


# ---------------------------------------------------------------- opening


async def test_the_query_box_is_focused_on_open(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#search-query", Input).has_focus


# ---------------------------------------------------------------- searching


async def test_results_appear_as_you_type(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS", "CUSTOMERS", "ORDER_ITEMS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
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


async def test_clearing_the_query_empties_the_results(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        await type_query(pilot, app, "")
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


async def test_searching_without_a_database_is_empty_not_an_error(repo) -> None:
    app = Harness(repo, database=None)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "ord")
        assert shown(app) == []


# --------------------------------------------------------------- selection


async def test_enter_chooses_the_top_hit_and_closes_the_dialog(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS", "ORDER_ITEMS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "orders")
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected[0].name == "ORDERS"


async def test_a_chosen_result_carries_its_kind(repo) -> None:
    put(repo, ObjectKind.PIPE, "RAW", "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "load")
        app.screen._choose(0)
        await pilot.pause()
    assert app.selected[0].kind is ObjectKind.PIPE


async def test_escape_cancels_without_choosing(repo) -> None:
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
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
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    calls: list[str] = []
    original = repo.search_corpus

    def spy(database):
        calls.append(database)
        return original(database)

    repo.search_corpus = spy
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await type_query(pilot, app, "o")
        await type_query(pilot, app, "or")
        await type_query(pilot, app, "ord")
    assert calls == ["MYDB"]


async def test_objects_added_after_opening_are_invisible_until_the_dialog_reopens(
    repo,
) -> None:
    """The corpus is a snapshot taken when the dialog opens; see search.py for
    why a cache update reaching it while it is open is not on the table."""
    put(repo, ObjectKind.TABLE, "SALES", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        put(repo, ObjectKind.TABLE, "SALES", "ORDERS", "CUSTOMERS")
        await type_query(pilot, app, "cust")
        assert shown(app) == []
