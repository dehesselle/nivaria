"""Detail view for pipes.

Deliberately a different shape from the column views: a pipe has no columns,
its interesting content is a COPY statement and its running state, and the one
thing worth surfacing loudly is `invalid_reason` -- a pipe that has stopped
ingesting looks exactly like a healthy one until you read it.

That difference is the point: it is what the registry seam has to accommodate,
and what a single shared detail widget could not.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.widgets import Label, Static

from snowcone.models import ObjectKind
from snowcone.ui.views.base import ObjectView, register_view


def _field(row: dict, name: str) -> str:
    value = row.get(name)
    return "" if value is None else str(value)


@register_view
class PipeView(ObjectView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.PIPE

    def compose(self) -> ComposeResult:
        yield Static("", id="pipe-problem")
        yield Static("", id="pipe-facts")
        yield Label("Definition", id="pipe-definition-label")
        yield Static("", id="pipe-definition")

    def compose_context(self) -> ComposeResult:
        yield Label(self.ref.fqn, id="context-title")
        yield Static(self._status(), id="context-detail")

    def update_content(self) -> None:
        row = self.repo.object_detail(self.ref) or {}

        problem = self.query_one("#pipe-problem", Static)
        reason = _field(row, "invalid_reason")
        problem.update(f"PIPE STOPPED: {reason}" if reason else "")
        problem.display = bool(reason)

        facts = [
            ("Channel", _field(row, "notification_channel")),
            ("Integration", _field(row, "integration")),
            ("Pattern", _field(row, "pattern")),
            ("Owner", _field(row, "owner")),
            ("Created", _field(row, "created_on")),
        ]
        self.query_one("#pipe-facts", Static).update(
            "\n".join(f"{label:<14}{value}" for label, value in facts if value)
        )
        self.query_one("#pipe-definition", Static).update(_field(row, "definition"))

    def _status(self) -> str:
        row = self.repo.object_detail(self.ref)
        if row is None:
            return "not cached"
        reason = _field(row, "invalid_reason")
        return f"stopped: {reason}" if reason else "running"
