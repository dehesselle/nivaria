"""Tree behaviour, above all: an update never moves the cursor."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectKind,
    objects_scope,
    schemas_scope,
)
from snowcone.ui.messages import ObjectSelected, ScopeNeeded
from snowcone.ui.widgets.object_tree import ObjectTree


class Harness(App[None]):
    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self.repo = repo
        self.needed: list[tuple[str, bool]] = []

    def compose(self) -> ComposeResult:
        yield ObjectTree(self.repo, id="tree")

    def on_scope_needed(self, event: ScopeNeeded) -> None:
        self.needed.append((event.scope.key, event.interactive))


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def put_schemas(repo: Repository, database: str, *names: str) -> None:
    load(
        repo,
        schemas_scope(database),
        [{"database_name": database, "name": name, "comment": ""} for name in names],
    )


def put_tables(repo: Repository, database: str, schema: str, *names: str) -> None:
    load(
        repo,
        objects_scope(ObjectKind.TABLE, database, schema),
        [
            {
                "database_name": database,
                "schema_name": schema,
                "name": name,
                "kind": "TABLE",
                "comment": "",
            }
            for name in names
        ],
    )


def keys(node) -> list[str]:
    return [child.data.key for child in node.children]


async def open_tree(app: Harness, pilot, database: str = "MYDB") -> ObjectTree:
    tree = app.query_one(ObjectTree)
    tree.set_database(database)
    await pilot.pause()
    return tree


def schemas_node(tree: ObjectTree):
    return tree.root.children[0]


# ------------------------------------------------------------------ shape


async def test_a_database_starts_with_a_counted_schemas_node(repo) -> None:
    put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        assert schemas_node(tree).label.plain == "Schemas (3)"


async def test_schemas_appear_only_once_expanded(repo) -> None:
    """Counts come from the cache, so a collapsed node is correct without
    materialising anything under it."""
    put_schemas(repo, "MYDB", "AAA", "BBB")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        assert len(node.children) == 0
        node.expand()
        await pilot.pause()
        assert keys(node) == ["AAA", "BBB"]


async def test_a_schema_offers_a_category_per_object_kind(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "T1", "T2")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        schema = schemas_node(tree).children[0]
        schema.expand()
        await pilot.pause()
        assert keys(schema) == ["TABLE", "VIEW", "PIPE", "STAGE", "TASK"]
        assert schema.children[0].label.plain == "Tables (2)"


async def test_objects_appear_under_their_category(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS", "CUSTOMERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        schema = schemas_node(tree).children[0]
        schema.expand()
        await pilot.pause()
        tables = schema.children[0]
        tables.expand()
        await pilot.pause()
        assert keys(tables) == ["CUSTOMERS", "ORDERS"]


# ------------------------------------------------------------ cursor safety


async def test_an_unchanged_refresh_does_not_move_the_cursor(repo) -> None:
    """The common TTL outcome: nothing changed, so nothing is touched."""
    put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        tree.move_cursor(node.children[2])
        await pilot.pause()
        assert tree.cursor_node.data.key == "CCC"

        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()
        assert tree.cursor_node.data.key == "CCC"


async def test_removing_another_node_leaves_the_cursor_alone(repo) -> None:
    """This is the complaint the whole design answers: drilling down and then
    having an update throw the cursor back to the top."""
    put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        tree.move_cursor(node.children[2])
        await pilot.pause()

        put_schemas(repo, "MYDB", "BBB", "CCC")  # AAA dropped
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()

        assert keys(node) == ["BBB", "CCC"]
        assert tree.cursor_node.data.key == "CCC"


async def test_an_added_node_leaves_the_cursor_alone(repo) -> None:
    put_schemas(repo, "MYDB", "AAA", "BBB")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        tree.move_cursor(node.children[0])
        await pilot.pause()

        # ZZZ sorts last, so this is the in-place append path.
        put_schemas(repo, "MYDB", "AAA", "BBB", "ZZZ")
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()

        assert keys(node) == ["AAA", "BBB", "ZZZ"]
        assert tree.cursor_node.data.key == "AAA"


async def test_a_mid_order_insert_rebuilds_but_restores_the_cursor(repo) -> None:
    """The one path where node objects are destroyed, so the cursor has to be
    found again by key path rather than by line or id."""
    put_schemas(repo, "MYDB", "AAA", "CCC")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        tree.move_cursor(node.children[1])
        await pilot.pause()
        assert tree.cursor_node.data.key == "CCC"

        put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()
        await pilot.pause()

        assert keys(node) == ["AAA", "BBB", "CCC"]
        assert tree.cursor_node.data.key == "CCC"


async def test_the_cursor_survives_a_rebuild_while_deep_in_the_tree(repo) -> None:
    put_schemas(repo, "MYDB", "AAA", "CCC")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        schema = node.children[0]
        schema.expand()
        await pilot.pause()
        tables = schema.children[0]
        tables.expand()
        await pilot.pause()
        tree.move_cursor(tables.children[0])
        await pilot.pause()
        assert tree.cursor_node.data.key == "ORDERS"

        # A new schema in the middle rebuilds the level above the cursor.
        put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()
        await pilot.pause()

        assert tree.cursor_node is not None
        assert tree.cursor_node.data.key == "ORDERS"


async def test_removing_the_cursors_own_node_lands_on_the_next_sibling(repo) -> None:
    """Textual's _cursor_node would otherwise dangle at a detached node whose
    stale line decides where the cursor ends up."""
    put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        tree.move_cursor(node.children[1])
        await pilot.pause()

        put_schemas(repo, "MYDB", "AAA", "CCC")  # BBB, under the cursor, is gone
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()

        assert tree.cursor_node is not None
        assert tree.cursor_node.data.key == "CCC"


async def test_removing_the_last_node_falls_back_to_the_previous(repo) -> None:
    put_schemas(repo, "MYDB", "AAA", "BBB")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        tree.move_cursor(node.children[1])
        await pilot.pause()

        put_schemas(repo, "MYDB", "AAA")
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()

        assert tree.cursor_node.data.key == "AAA"


async def test_expansion_survives_a_rebuild(repo) -> None:
    """A rebuild that silently collapsed everything would lose the user's
    place just as surely as moving the cursor."""
    put_schemas(repo, "MYDB", "AAA", "CCC")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        node = schemas_node(tree)
        node.expand()
        await pilot.pause()
        node.children[0].expand()
        await pilot.pause()

        put_schemas(repo, "MYDB", "AAA", "BBB", "CCC")
        tree.cache_updated(schemas_scope("MYDB"))
        await pilot.pause()
        await pilot.pause()

        assert node.children[0].data.key == "AAA"
        assert node.children[0].is_expanded


# ----------------------------------------------------------------- counts


async def test_a_count_change_only_relabels(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "T1")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        schema = schemas_node(tree).children[0]
        schema.expand()
        await pilot.pause()
        tables = schema.children[0]
        assert tables.label.plain == "Tables (1)"

        put_tables(repo, "MYDB", "AAA", "T1", "T2")
        tree.cache_updated(objects_scope(ObjectKind.TABLE, "MYDB", "AAA"))
        await pilot.pause()
        assert tables.label.plain == "Tables (2)"


# ------------------------------------------------------------- fetch intent


async def test_an_empty_scope_is_requested_interactively(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await open_tree(app, pilot)
        await pilot.pause()
    assert ("schemas:MYDB", True) in app.needed


async def test_a_fresh_scope_is_not_requested(repo) -> None:
    """Every request costs a round trip, so cached-and-fresh must be silent."""
    put_schemas(repo, "MYDB", "AAA")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        await open_tree(app, pilot)
        await pilot.pause()
    assert app.needed == []


async def test_expanding_a_schema_asks_for_each_category(repo) -> None:
    """All five categories become visible at once, so their counts all have to
    be right -- but the user did not ask for any one of them specifically."""
    put_schemas(repo, "MYDB", "AAA")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        schemas_node(tree).children[0].expand()
        await pilot.pause()
    requested = dict(app.needed)
    assert requested["tables:MYDB.AAA"] is False
    assert requested["pipes:MYDB.AAA"] is False


async def test_only_materialised_scopes_are_offered_to_the_sweep(repo) -> None:
    """Refreshing something nobody has opened is work nobody is waiting on."""
    put_schemas(repo, "MYDB", "AAA", "BBB")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        assert [s.key for s in tree.visible_scopes()] == ["schemas:MYDB"]
        schemas_node(tree).expand()
        await pilot.pause()
        schemas_node(tree).children[0].expand()
        await pilot.pause()
        scopes = {s.key for s in tree.visible_scopes()}
    assert "tables:MYDB.AAA" in scopes
    assert "tables:MYDB.BBB" not in scopes


# --------------------------------------------------------------- selection


async def test_highlighting_an_object_announces_it(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    seen: list[str] = []

    class Listening(Harness):
        def on_object_selected(self, event: ObjectSelected) -> None:
            seen.append(event.ref.fqn)

    app = Listening(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        schema = schemas_node(tree).children[0]
        schema.expand()
        await pilot.pause()
        tables = schema.children[0]
        tables.expand()
        await pilot.pause()
        tree.move_cursor(tables.children[0])
        await pilot.pause()
    assert "MYDB.AAA.ORDERS" in seen


async def test_a_category_node_announces_nothing(repo) -> None:
    """Only leaf objects have a detail view; a category is navigation."""
    put_schemas(repo, "MYDB", "AAA")
    seen: list[str] = []

    class Listening(Harness):
        def on_object_selected(self, event: ObjectSelected) -> None:
            seen.append(event.ref.fqn)

    app = Listening(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        tree.move_cursor(schemas_node(tree).children[0])
        await pilot.pause()
    assert seen == []


# ------------------------------------------------------------ db switching


async def test_switching_database_clears_the_tree(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_schemas(repo, "OTHER", "ZZZ")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        assert keys(schemas_node(tree)) == ["AAA"]

        tree.set_database("OTHER")
        await pilot.pause()
        schemas_node(tree).expand()
        await pilot.pause()
        assert keys(schemas_node(tree)) == ["ZZZ"]
        assert [s.key for s in tree.visible_scopes()] == ["schemas:OTHER"]


async def test_an_object_name_with_markup_is_not_interpreted(repo) -> None:
    """A bare str label would have [beta] eaten as console markup."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS[beta]")
    app = Harness(repo)
    async with app.run_test(size=(80, 30)) as pilot:
        tree = await open_tree(app, pilot)
        schemas_node(tree).expand()
        await pilot.pause()
        schema = schemas_node(tree).children[0]
        schema.expand()
        await pilot.pause()
        tables = schema.children[0]
        tables.expand()
        await pilot.pause()
        assert tables.children[0].label.plain == "ORDERS[beta]"
