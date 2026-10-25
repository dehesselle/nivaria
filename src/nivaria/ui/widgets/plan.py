# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Working out what has to change in a node's children.

Pure: no Textual, no I/O. The whole diffing decision lives here so it can be
tested against plain lists, leaving the widget code to do nothing but carry the
plan out.

The shape of this is dictated by one Textual constraint: `TreeNode` can append
(`add`), remove and relabel, but there is **no insert-at-index**. So an object
that appears in the middle of a sorted list cannot be put in its place; the
only correct response is to rebuild the whole child list. Everything else --
removals, relabels, and additions that happen to belong at the end -- can be
done in place, which matters because in-place changes keep the cursor where
the user left it for free.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Child:
    """One desired child: a stable identity plus how it should read."""

    key: str
    label: str
    expandable: bool = False


@dataclass(frozen=True)
class Plan:
    """How to get a node's children from what they are to what they should be.

    When `rebuild` is set the other fields are empty: the caller replaces the
    children wholesale from the desired list, so an itemised diff would only
    be a second, contradictory description of the same change.
    """

    removed: tuple[str, ...] = ()
    relabelled: tuple[Child, ...] = ()
    appended: tuple[Child, ...] = ()
    rebuild: bool = False

    @property
    def is_noop(self) -> bool:
        return not (self.removed or self.relabelled or self.appended or self.rebuild)


@dataclass(frozen=True)
class Existing:
    key: str
    label: str = field(default="")


def plan_children(
    existing: Sequence[tuple[str, str]],
    desired: Sequence[Child],
) -> Plan:
    """Diff current `(key, label)` children against the desired children.

    The common case by far is a TTL refresh that finds nothing changed, which
    returns a no-op plan and therefore touches nothing at all -- the cursor
    cannot move if no node is mutated.
    """
    existing_labels = {key: label for key, label in existing}
    existing_keys = [key for key, _ in existing]
    existing_set = set(existing_keys)
    desired_keys = [child.key for child in desired]
    desired_set = set(desired_keys)

    removed = tuple(key for key in existing_keys if key not in desired_set)
    survivors = [key for key in existing_keys if key in desired_set]
    added = tuple(child for child in desired if child.key not in existing_set)

    # Appending can only reach the desired order if every new child belongs
    # after every survivor. Otherwise the order after removals and appends
    # would not match, and without insert-at-index a rebuild is the only way.
    if [*survivors, *(child.key for child in added)] != desired_keys:
        return Plan(rebuild=True)

    relabelled = tuple(
        child
        for child in desired
        if child.key in existing_set and existing_labels[child.key] != child.label
    )
    return Plan(removed=removed, relabelled=relabelled, appended=added)
