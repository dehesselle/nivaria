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
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from snowcone.cache.repository import Repository
from snowcone.format import format_count
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    Scope,
    objects_scope,
    schemas_scope,
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

SCHEMAS_KEY = "__schemas__"


class NodeKind(StrEnum):
    SCHEMAS = "schemas"
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


class ObjectTree(Tree[NodeData]):
    """A lazy tree over the cached metadata of one database."""

    class ObjectSelected(Message):
        """A browsable object is under the cursor."""

        def __init__(self, ref: ObjectRef) -> None:
            self.ref = ref
            super().__init__()

    class ScopeNeeded(Message):
        """This scope is missing or stale and something is showing it.

        Posted rather than calling the fetch service directly so the tree
        stays ignorant of scheduling, and so a test can assert on intent.
        """

        def __init__(self, scope: Scope, *, interactive: bool) -> None:
            self.scope = scope
            self.interactive = interactive
            super().__init__()

    def __init__(self, repo: Repository, *, ttl: float = 900.0, **kwargs) -> None:
        super().__init__("root", data=NodeData(NodeKind.SCHEMAS, "root"), **kwargs)
        self.repo = repo
        self.ttl = ttl
        self.show_root = False
        self.database: str | None = None
        #: Nodes whose children come from a scope, so an update can be routed
        #: to exactly one node instead of rebuilding the tree.
        self._scoped: dict[Scope, TreeNode[NodeData]] = {}
        #: Set while this widget is restructuring, so the NodeHighlighted that
        #: a cursor restore emits is not mistaken for a user selection.
        self.rebuilding = False

    # ------------------------------------------------------------- database

    def set_database(self, database: str | None) -> None:
        """Point the tree at a database, discarding whatever it showed."""
        self.database = database
        self.unselect()
        self.root.remove_children()
        self._scoped.clear()
        if database is None:
            return
        scope = schemas_scope(database)
        node = self.root.add(
            self._schemas_label(scope),
            data=NodeData(NodeKind.SCHEMAS, SCHEMAS_KEY, scope),
        )
        self._scoped[scope] = node
        self._ensure(scope, interactive=True)

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
        if data is not None and data.ref is not None:
            self.post_message(self.ObjectSelected(data.ref))

    # ------------------------------------------------------------ freshness

    def _ensure(self, scope: Scope, *, interactive: bool) -> None:
        entry = self.repo.entry(scope)
        age = entry.age()
        if age is None or (self.ttl > 0 and age >= self.ttl):
            self.post_message(self.ScopeNeeded(scope, interactive=interactive))

    def visible_scopes(self) -> list[Scope]:
        """Scopes currently materialised, for the TTL sweep to consider.

        Refreshing anything else would be work nobody is waiting on.
        """
        return list(self._scoped)

    # --------------------------------------------------------------- labels

    def _schemas_label(self, scope: Scope) -> Text:
        return Text(f"Schemas {format_count(self.repo.child_count(scope))}")

    def _category_label(self, kind: ObjectKind, scope: Scope) -> Text:
        return Text(f"{kind.title} {format_count(self.repo.child_count(scope))}")

    def _relabel(self, node: TreeNode[NodeData]) -> None:
        data = node.data
        if data is None or data.scope is None:
            return
        if data.kind is NodeKind.SCHEMAS:
            node.set_label(self._schemas_label(data.scope))
        elif data.kind is NodeKind.CATEGORY and data.scope.object_kind is not None:
            node.set_label(self._category_label(data.scope.object_kind, data.scope))

    # ------------------------------------------------------------- children

    def _desired(self, node: TreeNode[NodeData]) -> list[Child]:
        """What this node's children should be, straight from the cache."""
        data = node.data
        if data is None or self.database is None:
            return []
        match data.kind:
            case NodeKind.SCHEMAS:
                assert data.scope is not None
                return [
                    Child(key=schema.name, label=schema.name, expandable=True)
                    for schema in self.repo.schemas(data.scope).rows
                ]
            case NodeKind.SCHEMA:
                return [
                    Child(
                        key=kind.value,
                        label=self._category_label(
                            kind, objects_scope(kind, self.database, data.key)
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
            case NodeKind.OBJECT:
                return []

    def _child_data(self, parent: TreeNode[NodeData], child: Child) -> NodeData:
        data = parent.data
        assert data is not None
        assert self.database is not None
        match data.kind:
            case NodeKind.SCHEMAS:
                return NodeData(NodeKind.SCHEMA, child.key)
            case NodeKind.SCHEMA:
                kind = ObjectKind(child.key)
                return NodeData(
                    NodeKind.CATEGORY,
                    child.key,
                    scope=objects_scope(kind, self.database, data.key),
                )
            case _:
                assert data.scope is not None
                assert data.scope.object_kind is not None
                assert data.scope.schema is not None
                return NodeData(
                    NodeKind.OBJECT,
                    child.key,
                    ref=ObjectRef(
                        kind=data.scope.object_kind,
                        database=self.database,
                        schema=data.scope.schema,
                        name=child.key,
                    ),
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
