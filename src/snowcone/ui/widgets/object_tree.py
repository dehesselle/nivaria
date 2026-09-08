"""The object browser tree, driven entirely from the cache.

Every read here comes from sqlite, so expanding a node paints immediately even
on a cold cache; if what came back is stale or missing, a fetch is *requested*
and the answer arrives later as a `cache_updated` call. Nothing on this path
waits for Snowflake.

Cursor stability
----------------
The requirement is that an update never moves the cursor off what the user was
looking at. Textual gives most of this for free, but only if you know where the
line is:

* `Tree._cursor_node` holds a *reference to the node*, and `_build` recomputes
  `cursor_line` from `cursor_node._line`. So removing, relabelling or appending
  **other** nodes keeps the cursor exactly where it was, with no work at all.
* But `_cursor_node` is only reassigned by `watch_cursor_line`. Remove the node
  the cursor is on and the reference dangles, pointing at a detached node whose
  stale `_line` then decides where the cursor lands. So the cursor has to be
  moved off a node *before* that node is removed.
* A rebuild (`remove_children` + re-add) destroys every node object, so the
  cursor is restored afterwards by *key path* -- node ids and line numbers mean
  nothing across a rebuild, but ("MYDB", "PUBLIC", "tables", "ORDERS") does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rich.text import Text
from textual import events
from textual.geometry import Region
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from snowcone.cache.repository import Repository
from snowcone.format import format_count
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    SchemaRef,
    Scope,
    objects_scope,
    schemas_scope,
)
from snowcone.ui.messages import (
    CategorySelected,
    DatabaseSelected,
    ObjectSelected,
    SchemaSelected,
    ScopeNeeded,
)
from snowcone.ui.widgets.plan import Child, plan_children

#: Which object kinds get a category node under each schema, in display order.
TREE_KINDS: tuple[ObjectKind, ...] = (
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.PIPE,
    ObjectKind.STAGE,
    ObjectKind.TASK,
)


class NodeKind(StrEnum):
    #: The hidden root. Its children are one DATABASE node per selected
    #: database, flowing through the same _desired/_apply machinery as every
    #: other level -- see set_databases.
    ROOT = "root"
    #: Was SCHEMAS: merged with the old top-level "Schemas (n)" node, since
    #: the database itself is now the top-level node -- it shows the schema
    #: count directly and expands straight into schemas, one less level of
    #: nesting than a separate "Schemas" child would need.
    DATABASE = "database"
    SCHEMA = "schema"
    CATEGORY = "category"
    OBJECT = "object"


@dataclass(frozen=True)
class NodeData:
    kind: NodeKind
    key: str
    #: The cache scope whose rows fill this node's *children*, if any.
    scope: Scope | None = None
    ref: ObjectRef | None = None
    #: Which database this node belongs to -- set explicitly at every level
    #: below ROOT (denormalized the same way `ref` already is, rather than
    #: derived by walking to the ancestor DATABASE node on every read).
    database: str | None = None


class ObjectTree(Tree[NodeData]):
    """A lazy tree over the cached metadata of every selected database."""

    #: Guard rails for the drag-resize below -- just enough that neither this
    #: pane nor its DetailPane sibling can be dragged into uselessness.
    #: Ported unchanged from snakecam's ResizableTree (same mechanism, same
    #: numbers -- see ../snakecam/src/snakecam/screens/widgets.py).
    _MIN_WIDTH = 15
    _MIN_SIBLING_WIDTH = 20

    def __init__(self, repo: Repository, *, ttl: float = 900.0, **kwargs) -> None:
        super().__init__("root", data=NodeData(NodeKind.ROOT, "root"), **kwargs)
        self.repo = repo
        self.ttl = ttl
        self.show_root = False
        self.databases: list[str] = []
        #: Nodes whose children come from a scope, so an update can be routed
        #: to exactly one node instead of rebuilding the tree.
        self._scoped: dict[Scope, TreeNode[NodeData]] = {}
        #: Set while this widget is restructuring, so the NodeHighlighted that
        #: a cursor restore emits is not mistaken for a user selection.
        self.rebuilding = False
        #: Whether the right border is currently being dragged to resize.
        self._dragging = False

    # ------------------------------------------------------------- resize

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # The tree's own `border: round` right edge is the drag target --
        # there is no separate handle widget, same as snakecam's ResizableTree.
        if event.x != self.outer_size.width - 1:
            return
        self._dragging = True
        self.capture_mouse()
        self.styles.pointer = "ew-resize"
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        # Captured, so event.x stays relative to this widget's own,
        # unmoving left edge -- no delta tracking needed.
        max_width = self.screen.size.width - self._MIN_SIBLING_WIDTH
        self.styles.width = max(self._MIN_WIDTH, min(event.x + 1, max_width))
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.release_mouse()
        self.styles.pointer = "default"
        event.stop()

    # ----------------------------------------------------------------- repo

    def set_repo(self, repo: Repository) -> None:
        """Point at a different cache entirely -- a role switch, or a
        reconnect to a different identity. Whatever this showed was read out
        of the old identity's cache file and cannot simply be relabelled
        against the new one."""
        self.repo = repo
        self.set_databases([])

    # ------------------------------------------------------------ databases

    def set_databases(self, databases: list[str]) -> None:
        """Point the tree at this set of databases.

        Unlike the old single-database set_database, this does not
        unconditionally wipe the tree: root's children now flow through the
        same _desired/_apply diff every other level already uses (see
        DESIGN.md "The cursor must not move"), so picking the same set again
        is a no-op, adding a database appends a node without disturbing the
        rest, and removing one moves the cursor clear of it first -- all for
        free, the same machinery already built for schema/object children.
        """
        self.databases = list(databases)
        self._apply(self.root, self._desired(self.root))

    def _walk(self, path: tuple[str, ...]) -> TreeNode[NodeData] | None:
        """Materialise and descend to the node at `path` from the root.

        Shared by `reveal` and `reveal_schema`: every level below the root
        has to be expanded and materialised from the cache on the way down,
        since a collapsed node's children were never fetched from sqlite.
        Synchronous throughout -- everything walked is already cached, so
        there is nothing here to wait on.
        """
        node = self.root
        for key in path:
            if node is not self.root:
                node.expand()
                self._apply(node, self._desired(node))
            for child in node.children:
                if child.data is not None and child.data.key == key:
                    node = child
                    break
            else:
                return None
        return node

    def reveal(self, ref: ObjectRef) -> bool:
        """Expand down to an object and put the cursor on it.

        So a search hit leaves you somewhere you can keep browsing from,
        rather than showing a detail pane with the tree still elsewhere.
        """
        if ref.database not in self.databases:
            return False
        node = self._walk((ref.database, ref.schema, ref.kind.value, ref.name))
        if node is None:
            return False
        # Not an immediate move_cursor: the node just materialised on the way
        # down still has _line == -1 until the next refresh, so the cursor
        # would silently land on line 0 instead of on the object.
        self.call_after_refresh(self.move_cursor, node)
        return True

    def reveal_schema(self, ref: SchemaRef) -> bool:
        """Expand down to a schema, expand it too, and put the cursor on it.

        A schema search hit has no object to land on and no detail view to
        open (see `SchemaRef`), so unlike `reveal` this stops one level
        higher and expands the schema node itself -- materialising its
        category children -- rather than descending into one of them.
        """
        if ref.database not in self.databases:
            return False
        node = self._walk((ref.database, ref.name))
        if node is None:
            return False
        node.expand()
        self._apply(node, self._desired(node))
        self.call_after_refresh(self._settle_on_schema, node)
        return True

    def _settle_on_schema(self, node: TreeNode[NodeData]) -> None:
        """Put the cursor on `node` and scroll so its just-expanded category
        children are visible too, not only the schema's own line.

        `move_cursor`'s default scroll (`Tree.scroll_to_node`) treats the
        cursor as a single line and scrolls the least distance to bring only
        that line into view -- for a downward reveal that pins the schema at
        the very last visible row, pushing every category it just gained off
        the bottom of the screen. Scrolling to a region spanning the schema
        and its children instead keeps both ends in view when they fit, and
        anchors on the schema's own line rather than the last child when they
        do not (`Region.crop_size`, which the scroll math crops through,
        keeps a region's top-left and only shrinks toward it).
        """
        self.move_cursor(node)
        label = self._get_label_region(node._line)
        if label is None:
            return
        span = Region(label.x, label.y, label.width, label.height + len(node.children))
        self.scroll_to_region(
            span, animate=False, force=True, origin_visible=False, x_axis=False
        )

    # ---------------------------------------------------------- cache input

    def cache_updated(self, scope: Scope) -> None:
        """Reconcile only the node this scope backs."""
        node = self._scoped.get(scope)
        if node is None:
            return
        self._relabel(node)
        # Collapsed and never opened means no children to reconcile;
        # materialising them now would defeat the lazy loading.
        if node.children or node.is_expanded:
            self._apply(node, self._desired(node))

    # -------------------------------------------------------------- events

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[NodeData]) -> None:
        node = event.node
        self._apply(node, self._desired(node))
        data = node.data
        if data is None:
            return
        if data.scope is not None:
            self._ensure(data.scope, interactive=True)
        # A schema's categories each have their own scope, and all of them are
        # about to be on screen, so their counts need to be right too.
        if data.kind is NodeKind.SCHEMA:
            for child in node.children:
                if child.data is not None and child.data.scope is not None:
                    self._ensure(child.data.scope, interactive=False)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[NodeData]) -> None:
        if self.rebuilding:
            # Mid-restructure; the cursor restore decides what is selected.
            return
        data = event.node.data
        if data is None:
            return
        if data.ref is not None:
            self.post_message(ObjectSelected(data.ref))
        elif data.kind is NodeKind.CATEGORY and data.scope is not None:
            self.post_message(CategorySelected(data.scope))
        elif data.kind is NodeKind.SCHEMA and data.database is not None:
            self.post_message(
                SchemaSelected(SchemaRef(database=data.database, name=data.key))
            )
        elif data.kind is NodeKind.DATABASE and data.database is not None:
            self.post_message(DatabaseSelected(data.database))

    # ------------------------------------------------------------ freshness

    def _ensure(self, scope: Scope, *, interactive: bool) -> None:
        entry = self.repo.entry(scope)
        age = entry.age()
        if age is None or (self.ttl > 0 and age >= self.ttl):
            self.post_message(ScopeNeeded(scope, interactive=interactive))

    def visible_scopes(self) -> list[Scope]:
        """Scopes currently materialised, for the TTL sweep to consider.

        Refreshing anything else would be work nobody is waiting on.
        """
        return list(self._scoped)

    # --------------------------------------------------------------- labels

    def _database_label(self, database: str) -> Text:
        return Text(
            f"{database} {format_count(self.repo.child_count(schemas_scope(database)))}"
        )

    def _category_label(self, kind: ObjectKind, scope: Scope) -> Text:
        return Text(f"{kind.title} {format_count(self.repo.child_count(scope))}")

    def _relabel(self, node: TreeNode[NodeData]) -> None:
        data = node.data
        if data is None:
            return
        if data.kind is NodeKind.DATABASE and data.database is not None:
            node.set_label(self._database_label(data.database))
        elif (
            data.kind is NodeKind.CATEGORY
            and data.scope is not None
            and data.scope.object_kind is not None
        ):
            node.set_label(self._category_label(data.scope.object_kind, data.scope))

    # ------------------------------------------------------------- children

    def _desired(self, node: TreeNode[NodeData]) -> list[Child]:
        """What this node's children should be, straight from the cache."""
        data = node.data
        if data is None:
            return []
        match data.kind:
            case NodeKind.ROOT:
                return [
                    Child(
                        key=database,
                        label=self._database_label(database).plain,
                        expandable=True,
                    )
                    for database in self.databases
                ]
            case NodeKind.OBJECT:
                return []
            case NodeKind.DATABASE:
                assert data.scope is not None
                return [
                    Child(key=schema.name, label=schema.name, expandable=True)
                    for schema in self.repo.schemas(data.scope).rows
                ]
            case NodeKind.SCHEMA:
                assert data.database is not None
                return [
                    Child(
                        key=kind.value,
                        label=self._category_label(
                            kind, objects_scope(kind, data.database, data.key)
                        ).plain,
                        expandable=True,
                    )
                    for kind in TREE_KINDS
                ]
            case NodeKind.CATEGORY:
                assert data.scope is not None
                return [
                    Child(key=obj.name, label=obj.name)
                    for obj in self.repo.objects(data.scope).rows
                ]

    def _child_data(self, parent: TreeNode[NodeData], child: Child) -> NodeData:
        data = parent.data
        assert data is not None
        match data.kind:
            case NodeKind.ROOT:
                return NodeData(
                    NodeKind.DATABASE,
                    child.key,
                    scope=schemas_scope(child.key),
                    database=child.key,
                )
            case NodeKind.DATABASE:
                assert data.database is not None
                return NodeData(NodeKind.SCHEMA, child.key, database=data.database)
            case NodeKind.SCHEMA:
                assert data.database is not None
                kind = ObjectKind(child.key)
                return NodeData(
                    NodeKind.CATEGORY,
                    child.key,
                    scope=objects_scope(kind, data.database, data.key),
                    database=data.database,
                )
            case _:
                assert data.scope is not None
                assert data.scope.object_kind is not None
                assert data.scope.schema is not None
                assert data.database is not None
                return NodeData(
                    NodeKind.OBJECT,
                    child.key,
                    ref=ObjectRef(
                        kind=data.scope.object_kind,
                        database=data.database,
                        schema=data.scope.schema,
                        name=child.key,
                    ),
                    database=data.database,
                )

    # ----------------------------------------------------------- the applier

    def _apply(self, node: TreeNode[NodeData], desired: list[Child]) -> None:
        existing = [
            (child.data.key if child.data else "", child.label.plain)
            for child in node.children
        ]
        plan = plan_children(existing, desired)
        if plan.is_noop:
            return
        if plan.rebuild:
            self._rebuild(node, desired)
            return

        if plan.removed:
            doomed = set(plan.removed)
            # Before removing, not after: Tree._cursor_node would otherwise be
            # left pointing at a detached node whose stale _line decides where
            # the cursor lands.
            self._move_cursor_clear_of(node, doomed)
            for child in list(node.children):
                if child.data is not None and child.data.key in doomed:
                    self._forget(child)
                    child.remove()

        if plan.relabelled:
            labels = {child.key: child.label for child in plan.relabelled}
            for child in node.children:
                if child.data is not None and child.data.key in labels:
                    child.set_label(Text(labels[child.data.key]))

        for child in plan.appended:
            self._add(node, child)

    def _add(self, parent: TreeNode[NodeData], child: Child) -> None:
        data = self._child_data(parent, child)
        # Text, not str: an object name containing brackets would otherwise be
        # eaten as console markup.
        label = Text(child.label)
        node = (
            parent.add(label, data=data, expand=False)
            if child.expandable
            else parent.add_leaf(label, data=data)
        )
        if data.scope is not None:
            self._scoped[data.scope] = node
        if data.kind is NodeKind.DATABASE:
            # A database node is always directly visible under the hidden
            # root, unlike a collapsed schema's categories -- so its schema
            # list is requested right away rather than waiting for an
            # expand, the same eagerness the old set_database gave its one
            # hand-built node.
            assert data.scope is not None
            self._ensure(data.scope, interactive=True)

    def _forget(self, node: TreeNode[NodeData]) -> None:
        """Drop a subtree from the scope index before it is removed."""
        if node.data is not None and node.data.scope is not None:
            self._scoped.pop(node.data.scope, None)
        for child in node.children:
            self._forget(child)

    def _rebuild(self, node: TreeNode[NodeData], desired: list[Child]) -> None:
        path = self._cursor_path()
        expanded = {
            child.data.key
            for child in node.children
            if child.data is not None and child.is_expanded
        }
        self.rebuilding = True
        self.unselect()
        for child in node.children:
            self._forget(child)
        node.remove_children()
        for child in desired:
            self._add(node, child)
        for child in node.children:
            if child.data is not None and child.data.key in expanded:
                child.expand()
        # A freshly built node's _line is still -1 until the next refresh, so
        # moving the cursor now would silently land on line 0.
        self.call_after_refresh(self._restore_cursor, path)

    # ---------------------------------------------------------- the cursor

    def _cursor_path(self) -> tuple[str, ...] | None:
        node = self.cursor_node
        if node is None:
            return None
        keys: list[str] = []
        while node is not None and node is not self.root:
            if node.data is None:
                return None
            keys.append(node.data.key)
            node = node.parent
        return tuple(reversed(keys))

    def _find(self, path: tuple[str, ...]) -> TreeNode[NodeData] | None:
        node = self.root
        for key in path:
            for child in node.children:
                if child.data is not None and child.data.key == key:
                    node = child
                    break
            else:
                return None
        return node

    def _restore_cursor(self, path: tuple[str, ...] | None) -> None:
        try:
            if path is not None:
                node = self._find(path)
                if node is not None:
                    self.move_cursor(node)
        finally:
            self.rebuilding = False

    def _move_cursor_clear_of(
        self, parent: TreeNode[NodeData], doomed: set[str]
    ) -> None:
        """Move the cursor off any node about to be removed.

        Lands on the next surviving sibling, else the previous one, else the
        parent -- never back at the top, which is the whole complaint this
        design exists to answer.
        """
        cursor = self.cursor_node
        if cursor is None:
            return
        if not self._inside_doomed(cursor, parent, doomed):
            return
        survivors = [
            child
            for child in parent.children
            if child.data is not None and child.data.key not in doomed
        ]
        if not survivors:
            self.move_cursor(parent if parent is not self.root else None)
            return
        removed_index = next(
            index
            for index, child in enumerate(parent.children)
            if child.data is not None and child.data.key in doomed
        )
        after = [
            child
            for index, child in enumerate(parent.children)
            if index > removed_index and child in survivors
        ]
        self.move_cursor(after[0] if after else survivors[-1])

    @staticmethod
    def _inside_doomed(
        cursor: TreeNode[NodeData], parent: TreeNode[NodeData], doomed: set[str]
    ) -> bool:
        node: TreeNode[NodeData] | None = cursor
        while node is not None:
            if (
                node.parent is parent
                and node.data is not None
                and node.data.key in doomed
            ):
                return True
            node = node.parent
        return False
