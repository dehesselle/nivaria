"""The settings dialog: a tree of setting groups on the left, the selected
group's editable settings on the right.

Ported from snakecam's settings dialog (`screens/settings_dialog.py` there),
minus instance-scoped settings and settings-actions -- snowcone's
`ConfigStore` has no equivalent of either yet, so this stays a straight
Setting-only port rather than carrying that machinery in unused.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Rule, Switch, Tree
from textual.widgets.tree import TreeNode

from snowcone.settings import Setting, all_settings, config
from snowcone.ui.screens.confirm import ConfirmScreen
from snowcone.ui.screens.dialog_base import DialogScreen


def _group_paths(settings: list[Setting[Any]]) -> set[str]:
    """Every group path that must exist as a tree node: each group itself,
    plus every ancestor prefix it implies -- so a dot-separated group like
    "Updates.Runner" would still produce an "Updates" node to hang it off,
    even with no settings of its own directly assigned to "Updates". None of
    snowcone's groups are dotted yet, but the tree costs nothing extra to
    support it, and it is exactly what the ported dialog already relies on.
    """
    paths: set[str] = set()
    for setting in settings:
        segments = setting.group.split(".")
        for i in range(1, len(segments) + 1):
            paths.add(".".join(segments[:i]))
    return paths


class SettingsScreen(DialogScreen[None]):
    """Tree of setting groups on the left, the selected group's editable
    settings on the right. Edits to the visible group are unsaved until
    Apply/OK; navigating to a different group with unsaved edits confirms
    before discarding them.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_node: TreeNode[str] | None = None
        self._current_group: str | None = None
        #: {setting.key: config.get(setting)} as of when the current group's
        #: items were last (re)rendered or applied -- what "dirty" compares
        #: against.
        self._baseline: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Settings", id="title")
            with Horizontal(id="settings-body"):
                yield Tree("Settings", id="settings-tree")
                with VerticalScroll(id="settings-content"):
                    yield Label("Select a settings group.", id="settings-placeholder")
            with Horizontal(id="buttons"):
                yield Button("Apply", compact=True, id="settings-apply")
                yield Button("OK", variant="primary", compact=True, id="settings-ok")
                yield Button("Cancel", compact=True, id="settings-cancel")

    def on_mount(self) -> None:
        tree = self.query_one("#settings-tree", Tree)
        tree.show_root = False
        self._build_tree(tree)
        tree.focus()
        first = next(iter(tree.root.children), None)
        if first is not None:
            # A just-built node's _line is still -1 until the next refresh,
            # so selecting immediately would silently land on line 0 instead
            # (the same quirk MainScreen._focus_ref_node works around).
            self.call_after_refresh(tree.select_node, first)

    def _build_tree(self, tree: Tree[str]) -> None:
        paths = _group_paths(all_settings())
        children_of: dict[str, list[str]] = defaultdict(list)
        for path in paths:
            parent = path.rsplit(".", 1)[0] if "." in path else ""
            children_of[parent].append(path)
        for kids in children_of.values():
            kids.sort(key=lambda p: p.rsplit(".", 1)[-1])

        def add_children(parent_node: TreeNode[str], parent_path: str) -> None:
            for path in children_of.get(parent_path, []):
                label = path.rsplit(".", 1)[-1]  # verbatim, no case change
                node = parent_node.add(label, data=path, expand=True)
                add_children(node, path)

        add_children(tree.root, "")

    def _settings_for(self, group: str) -> list[Setting[Any]]:
        return [s for s in all_settings() if s.group == group]

    # ------------------------------------------------------------- widgets

    def _widget_for(self, setting: Setting[Any]) -> Input | Switch:
        widget = self.query_one(f"#value-{setting.key}")
        assert isinstance(widget, Input | Switch)
        return widget

    def _make_value_widget(self, setting: Setting[Any]) -> Input | Switch:
        if isinstance(setting.default, bool):
            return Switch(value=config.get(setting), id=f"value-{setting.key}")
        return Input(
            value=str(config.get(setting)),
            placeholder=str(setting.default),
            id=f"value-{setting.key}",
        )

    async def _render_group(self, group: str) -> None:
        content = self.query_one("#settings-content", VerticalScroll)
        await content.remove_children()
        settings = self._settings_for(group)
        if not settings:
            # An ancestor node implied by a dotted group (e.g. "Updates" for
            # "Updates.Runner") has no settings of its own directly on it.
            await content.mount(
                Label("No settings in this group.", classes="settings-empty")
            )
            return
        for i, setting in enumerate(settings):
            if i > 0:
                await content.mount(Rule.horizontal())
            item = Vertical(
                Label(setting.description, classes="settings-item-description"),
                Horizontal(
                    Label(setting.label),
                    self._make_value_widget(setting),
                    classes="settings-item-field",
                ),
                classes="settings-item",
            )
            await content.mount(item)

    async def _select_group(self, node: TreeNode[str]) -> None:
        group = node.data
        assert group is not None
        await self._render_group(group)
        self._current_node = node
        self._current_group = group
        self._baseline = {s.key: config.get(s) for s in self._settings_for(group)}

    # -------------------------------------------------------------- editing

    def _current_value(self, setting: Setting[Any]) -> Any:
        """The value the visible widget currently represents, or raises
        ValueError if typed text doesn't parse."""
        widget = self._widget_for(setting)
        if isinstance(widget, Switch):
            return widget.value
        text = widget.value.strip()
        if not text:
            return setting.default  # empty means "reset to default"
        return setting.parse(text)

    def _is_dirty(self) -> bool:
        if self._current_group is None:
            return False
        for setting in self._settings_for(self._current_group):
            try:
                current = self._current_value(setting)
            except ValueError:
                # Can't prove it's unchanged; treat unparsed input as dirty
                # rather than silently letting it be discarded.
                return True
            if current != self._baseline[setting.key]:
                return True
        return False

    def _apply(self) -> bool:
        if self._current_group is None:
            return True
        settings = self._settings_for(self._current_group)
        parsed: dict[str, Any] = {}
        for setting in settings:
            try:
                parsed[setting.key] = self._current_value(setting)
            except ValueError as e:
                self.notify(
                    f"Invalid value for {setting.label}: {e}", severity="warning"
                )
                return False
        for setting in settings:
            config.set(setting, parsed[setting.key])
        self._baseline = parsed
        return True

    # --------------------------------------------------------------- events

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        event.stop()
        node = event.node
        if node.data is None or node is self._current_node:
            return
        self._handle_selection(node)

    @work(exclusive=True)
    async def _handle_selection(self, node: TreeNode[str]) -> None:
        # push_screen_wait requires an active worker context (that's what
        # this decorator provides) -- it cannot be awaited directly from a
        # plain message handler.
        if self._is_dirty():
            discard = await self.app.push_screen_wait(
                ConfirmScreen("You are about to lose unsaved changes!")
            )
            if not discard:
                if self._current_node is not None:
                    self.query_one("#settings-tree", Tree).move_cursor(
                        self._current_node
                    )
                return
        await self._select_group(node)

    def on_input_submitted(self) -> None:
        self._apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "settings-cancel":
            self.action_cancel()
        elif button_id == "settings-apply":
            self._apply()
        elif button_id == "settings-ok" and self._apply():
            self.dismiss(None)

    def action_cancel(self) -> None:
        self._cancel()

    @work(exclusive=True)
    async def _cancel(self) -> None:
        # push_screen_wait requires an active worker context (that's what
        # this decorator provides) -- it cannot be awaited directly from a
        # plain binding/button handler, hence the action_cancel split above,
        # the same pattern MainScreen.action_pick_database uses.
        if self._is_dirty():
            discard = await self.app.push_screen_wait(
                ConfirmScreen("You are about to lose unsaved changes!")
            )
            if not discard:
                return
        self.dismiss(None)
