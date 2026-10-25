# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

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
from tcoda.widgets import Resizable
from textual.geometry import Region
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from nivaria.cache.repository import Repository
from nivaria.format import format_count
from nivaria.models import (
    ObjectRef,
    ObjectType,
    SchemaRef,
    Scope,
    is_stale,
    objects_scope,
    schemas_scope,
)
from nivaria.settings import CACHE_TTL, config
from nivaria.ui.messages import (
    CategorySelected,
    DatabaseSelected,
    ObjectSelected,
    SchemaSelected,
    ScopeNeeded,
)
from nivaria.ui.widgets.plan import Child, plan_children

#: Which object types get a category node under each schema, in display order.
TREE_TYPES: tuple[ObjectType, ...] = (
    ObjectType.TABLE,
    ObjectType.VIEW,
    ObjectType.PIPE,
    ObjectType.STAGE,
    ObjectType.TASK,
)


class NodeType(StrEnum):
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
    type: NodeType
    key: str
    #: The cache scope whose rows fill this node's *children*, if any.
    scope: Scope | None = None
    ref: ObjectRef | None = None
    #: Which database this node belongs to -- set explicitly at every level
    #: below ROOT (denormalized the same way `ref` already is, rather than
    #: derived by walking to the ancestor DATABASE node on every read).
    database: str | None = None


class ObjectTree(Resizable, Tree[NodeData]):
    """A lazy tree over the cached metadata of every selected database."""

    #: Guard rails for `Resizable` -- just enough that neither this pane nor
    #: its DetailPane sibling can be dragged into uselessness. The drag
    #: itself lives in tcoda now, which is where snakecam's identical copy of
    #: it went too.
    RESIZE_MIN = 15
    RESIZE_MIN_SIBLING = 20

    def __init__(self, repo: Repository, **kwargs) -> None:
        super().__init__("root", data=NodeData(NodeType.ROOT, "root"), **kwargs)
        self.repo = repo
        self.show_root = False
        self.databases: list[str] = []
        #: Nodes whose children come from a scope, so an update can be routed
        #: to exactly one node instead of rebuilding the tree.
        self._scoped: dict[Scope, TreeNode[NodeData]] = {}
        #: Set while this widget is restructuring, so a highlight handled
        #: before the cursor restore runs is not mistaken for a user
        #: selection. The restore's own highlight is handled after this is
        #: cleared; `_restore_cursor` deals with that one.
        self.rebuilding = False
        #: The node whose highlight was last announced, so Textual's repeat
        #: highlight of an unmoved cursor is not announced twice -- see
        #: on_tree_node_highlighted.
        self._announced: TreeNode[NodeData] | None = None
        #: Where the latest reveal is taking the cursor, until its deferred
        #: move has run. A path, not a node: a rebuild landing in between
        #: destroys the node, and it restores the cursor here rather than to
        #: wherever it was before the jump.
        self._reveal_path: tuple[str, ...] | None = None

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
        docs/DESIGN.md "The cursor must not move"), so picking the same set again
        is a no-op, adding a database appends a node without disturbing the
        rest, and removing one moves the cursor clear of it first -- all for
        free, the same machinery already built for schema/object children.
        """
        self.databases = list(databases)
        # The caller clears the detail pane alongside this, so whatever the
        # cursor rests on next has to be announced again to refill it.
        self._announced = None
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
        path = (ref.database, ref.schema, ref.type.value, ref.name)
        if self._walk(path) is None:
            return False
        # Not an immediate move_cursor: the node just materialised on the way
        # down still has _line == -1 until the next refresh, so the cursor
        # would silently land on line 0 instead of on the object.
        self._reveal_path = path
        self.call_after_refresh(self._settle_on_object, path)
        return True

    def _claim_reveal(self, path: tuple[str, ...]) -> TreeNode[NodeData] | None:
        """The node a deferred reveal is landing on, found by path now.

        None when a later reveal has taken over the cursor since, or the
        node is gone.
        """
        if path != self._reveal_path:
            return None
        self._reveal_path = None
        return self._find(path)

    def _settle_on_object(self, path: tuple[str, ...]) -> None:
        """Put the cursor on `node` and scroll it to the middle of the tree.

        `move_cursor`'s default scroll scrolls the least distance to bring
        the line into view, which pins a hit further down at the very last
        visible row with nothing shown below it. Not `Tree.center_scroll`:
        that would center every arrow-key step too, not just the jump.
        """
        node = self._claim_reveal(path)
        if node is None:
            return
        self.move_cursor(node)
        label = self._get_label_region(node._line)
        if label is None:
            return
        self.scroll_to_region(
            label,
            animate=False,
            force=True,
            center=True,
            origin_visible=False,
            x_axis=False,
        )

    def reveal_schema(self, ref: SchemaRef) -> bool:
        """Expand down to a schema, expand it too, and put the cursor on it.

        A schema search hit has no object to land on and no detail view to
        open (see `SchemaRef`), so unlike `reveal` this stops one level
        higher and expands the schema node itself -- materialising its
        category children -- rather than descending into one of them.
        """
        if ref.database not in self.databases:
            return False
        path = (ref.database, ref.name)
        node = self._walk(path)
        if node is None:
            return False
        node.expand()
        self._apply(node, self._desired(node))
        self._reveal_path = path
        self.call_after_refresh(self._settle_on_schema, path)
        return True

    def _settle_on_schema(self, path: tuple[str, ...]) -> None:
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
        node = self._claim_reveal(path)
        if node is None:
            return
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
        if data.type is NodeType.SCHEMA:
            for child in node.children:
                if child.data is not None and child.data.scope is not None:
                    self._ensure(child.data.scope, interactive=False)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[NodeData]) -> None:
        if self.rebuilding:
            # Mid-restructure; the cursor restore decides what is selected.
            return
        if event.node is not self.cursor_node:
            # The cursor moved on before this was handled, and that move
            # posts its own highlight. Holding an arrow key would otherwise
            # repaint the detail pane for every object it passes.
            return
        if event.node is self._announced:
            # Not a move: Textual re-highlights the cursor's node whenever
            # its *line* changes (Tree._build reassigns cursor_line, and
            # watch_cursor_line compares against whichever node now holds
            # the old line). Announcing it again re-showed the same object.
            return
        data = event.node.data
        if data is None:
            return
        self._announced = event.node
        if data.ref is not None:
            self.post_message(ObjectSelected(data.ref))
        elif data.type is NodeType.CATEGORY and data.scope is not None:
            self.post_message(CategorySelected(data.scope))
        elif data.type is NodeType.SCHEMA and data.database is not None:
            self.post_message(
                SchemaSelected(SchemaRef(database=data.database, name=data.key))
            )
        elif data.type is NodeType.DATABASE and data.database is not None:
            self.post_message(DatabaseSelected(data.database))

    # ------------------------------------------------------------ freshness

    def _ensure(self, scope: Scope, *, interactive: bool) -> None:
        """Ask for `scope` if it is missing or stale.

        The TTL is read at the point of use rather than snapshotted at
        construction, so a change in the settings dialog reaches the tree
        without a restart -- and the decision itself is `models.is_stale`,
        shared with the detail pane and the background sweep, so a scope with
        a longer natural cadence is aged the same way wherever it is asked
        about.
        """
        if is_stale(scope, self.repo.entry(scope).age(), config.get(CACHE_TTL)):
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

    def _category_label(self, object_type: ObjectType, scope: Scope) -> Text:
        return Text(f"{object_type.title} {format_count(self.repo.child_count(scope))}")

    def _relabel(self, node: TreeNode[NodeData]) -> None:
        data = node.data
        if data is None:
            return
        if data.type is NodeType.DATABASE and data.database is not None:
            node.set_label(self._database_label(data.database))
        elif (
            data.type is NodeType.CATEGORY
            and data.scope is not None
            and data.scope.object_type is not None
        ):
            node.set_label(self._category_label(data.scope.object_type, data.scope))

    # ------------------------------------------------------------- children

    def _desired(self, node: TreeNode[NodeData]) -> list[Child]:
        """What this node's children should be, straight from the cache."""
        data = node.data
        if data is None:
            return []
        match data.type:
            case NodeType.ROOT:
                return [
                    Child(
                        key=database,
                        label=self._database_label(database).plain,
                        expandable=True,
                    )
                    for database in self.databases
                ]
            case NodeType.OBJECT:
                return []
            case NodeType.DATABASE:
                assert data.scope is not None
                return [
                    Child(key=schema.name, label=schema.name, expandable=True)
                    for schema in self.repo.schemas(data.scope).rows
                ]
            case NodeType.SCHEMA:
                assert data.database is not None
                return [
                    Child(
                        key=object_type.value,
                        label=self._category_label(
                            object_type,
                            objects_scope(object_type, data.database, data.key),
                        ).plain,
                        expandable=True,
                    )
                    for object_type in TREE_TYPES
                ]
            case NodeType.CATEGORY:
                assert data.scope is not None
                return [
                    Child(key=obj.name, label=obj.name)
                    for obj in self.repo.objects(data.scope).rows
                ]

    def _child_data(self, parent: TreeNode[NodeData], child: Child) -> NodeData:
        data = parent.data
        assert data is not None
        match data.type:
            case NodeType.ROOT:
                return NodeData(
                    NodeType.DATABASE,
                    child.key,
                    scope=schemas_scope(child.key),
                    database=child.key,
                )
            case NodeType.DATABASE:
                assert data.database is not None
                return NodeData(NodeType.SCHEMA, child.key, database=data.database)
            case NodeType.SCHEMA:
                assert data.database is not None
                object_type = ObjectType(child.key)
                return NodeData(
                    NodeType.CATEGORY,
                    child.key,
                    scope=objects_scope(object_type, data.database, data.key),
                    database=data.database,
                )
            case _:
                assert data.scope is not None
                assert data.scope.object_type is not None
                assert data.scope.schema is not None
                assert data.database is not None
                return NodeData(
                    NodeType.OBJECT,
                    child.key,
                    ref=ObjectRef(
                        type=data.scope.object_type,
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
        if data.type is NodeType.DATABASE:
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
        # A pending reveal owns the cursor: restoring it to where it was
        # before the jump would undo the jump.
        path = self._reveal_path or self._cursor_path()
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
                    announced = self._announced
                    if announced is not None and announced.data == node.data:
                        # The same object on a rebuilt node. Its highlight
                        # arrives after `rebuilding` is cleared below, so
                        # it is recognised as a repeat by identity instead.
                        self._announced = node
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
