from __future__ import annotations

from nivaria.ui.widgets.plan import Child, plan_children


def existing(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs)


def want(*items: str | tuple[str, str]) -> list[Child]:
    out: list[Child] = []
    for item in items:
        if isinstance(item, tuple):
            out.append(Child(key=item[0], label=item[1]))
        else:
            out.append(Child(key=item, label=item))
    return out


# ------------------------------------------------------------------ no-ops


def test_identical_children_are_a_noop() -> None:
    """The overwhelmingly common TTL-refresh outcome. Nothing is mutated, so
    the cursor cannot move -- that is the whole point of detecting it."""
    plan = plan_children(existing(("A", "A"), ("B", "B")), want("A", "B"))
    assert plan.is_noop


def test_two_empty_lists_are_a_noop() -> None:
    assert plan_children([], []).is_noop


# ------------------------------------------------------------- in-place work


def test_first_population_is_an_append_not_a_rebuild() -> None:
    """Expanding a node for the first time must not count as a rebuild, or
    every expand would pay the cursor-restore path."""
    plan = plan_children([], want("A", "B", "C"))
    assert plan.rebuild is False
    assert [c.key for c in plan.appended] == ["A", "B", "C"]


def test_a_new_last_child_is_appended() -> None:
    plan = plan_children(existing(("A", "A"), ("B", "B")), want("A", "B", "C"))
    assert plan.rebuild is False
    assert [c.key for c in plan.appended] == ["C"]
    assert plan.removed == ()


def test_a_removal_is_in_place() -> None:
    plan = plan_children(existing(("A", "A"), ("B", "B"), ("C", "C")), want("A", "C"))
    assert plan.rebuild is False
    assert plan.removed == ("B",)


def test_removing_everything_is_in_place() -> None:
    plan = plan_children(existing(("A", "A"), ("B", "B")), [])
    assert plan.rebuild is False
    assert plan.removed == ("A", "B")


def test_a_changed_label_is_a_relabel() -> None:
    """A count changing from Tables (5) to Tables (6) must not restructure
    anything -- the node is the same node."""
    plan = plan_children(
        existing(("tables", "Tables (5)")), want(("tables", "Tables (6)"))
    )
    assert plan.rebuild is False
    assert plan.removed == ()
    assert plan.appended == ()
    assert [(c.key, c.label) for c in plan.relabelled] == [("tables", "Tables (6)")]


def test_removals_relabels_and_appends_combine() -> None:
    plan = plan_children(
        existing(("A", "A"), ("B", "old"), ("C", "C")),
        want(("B", "new"), "C", "D"),
    )
    assert plan.rebuild is False
    assert plan.removed == ("A",)
    assert [c.key for c in plan.relabelled] == ["B"]
    assert [c.key for c in plan.appended] == ["D"]


# ---------------------------------------------------------------- rebuilds


def test_an_object_appearing_mid_order_forces_a_rebuild() -> None:
    """TreeNode has no insert-at-index, so a new B between A and C cannot be
    put in its place by appending."""
    plan = plan_children(existing(("A", "A"), ("C", "C")), want("A", "B", "C"))
    assert plan.rebuild is True


def test_a_rebuild_carries_no_itemised_diff() -> None:
    """Two contradictory descriptions of one change would be a trap for the
    applier; the desired list alone is authoritative."""
    plan = plan_children(existing(("A", "A"), ("C", "C")), want("A", "B", "C"))
    assert plan.removed == ()
    assert plan.appended == ()
    assert plan.relabelled == ()


def test_reordering_alone_forces_a_rebuild() -> None:
    plan = plan_children(existing(("A", "A"), ("B", "B")), want("B", "A"))
    assert plan.rebuild is True


def test_a_new_first_child_forces_a_rebuild() -> None:
    plan = plan_children(existing(("B", "B"), ("C", "C")), want("A", "B", "C"))
    assert plan.rebuild is True


def test_removal_plus_a_trailing_append_stays_in_place() -> None:
    """Removing from the middle does not by itself break append order."""
    plan = plan_children(existing(("A", "A"), ("B", "B")), want("A", "C"))
    assert plan.rebuild is False
    assert plan.removed == ("B",)
    assert [c.key for c in plan.appended] == ["C"]


def test_replacing_the_whole_list_rebuilds_only_if_order_demands_it() -> None:
    # Nothing survives, so appends land in exactly the desired order.
    plan = plan_children(existing(("A", "A")), want("X", "Y"))
    assert plan.rebuild is False
    assert plan.removed == ("A",)
    assert [c.key for c in plan.appended] == ["X", "Y"]


def test_keys_are_identity_labels_are_not() -> None:
    """A schema renamed in display terms is still the same node; a schema with
    a different key is a different node."""
    plan = plan_children(existing(("k", "one")), want(("k", "two")))
    assert plan.rebuild is False
    assert plan.removed == ()
