# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Snowsight links, in the shape Snowsight's own address bar shows."""

from __future__ import annotations

import pytest

from nivaria.gateway.pool import AccountNames
from nivaria.models import ObjectRef, ObjectType
from nivaria.snowsight import base_url, database_url, object_url, schema_url

NAMES = AccountNames("MYORG", "MYACCT")
BASE = "https://app.snowflake.com/myorg/myacct/"


def test_the_public_base_url_is_the_lowercased_names() -> None:
    assert base_url(NAMES, privatelink=False) == BASE


def test_an_account_name_keeps_its_underscores() -> None:
    names = AccountNames("MYORG", "MY_ACCT")
    assert base_url(names, privatelink=False) == (
        "https://app.snowflake.com/myorg/my_acct/"
    )


def test_a_privatelink_account_has_no_derivable_base_url() -> None:
    """Which private Snowsight host resolves depends on the network's DNS,
    which nothing on the client can see."""
    assert base_url(NAMES, privatelink=True) is None


def test_without_the_names_there_is_no_base_url() -> None:
    assert base_url(None, privatelink=False) is None


def test_the_setting_wins_and_gains_a_trailing_slash() -> None:
    override = "https://app.eu-central-1.privatelink.snowflakecomputing.com/o/a"
    assert base_url(NAMES, privatelink=True, override=override) == override + "/"
    assert base_url(None, privatelink=False, override=override + "/") == (
        override + "/"
    )


def test_a_blank_setting_is_no_setting() -> None:
    assert base_url(NAMES, privatelink=False, override="  ") == BASE


def test_database_and_schema_links() -> None:
    assert database_url(BASE, "DB") == BASE + "#/data/databases/DB"
    assert schema_url(BASE, "DB", "SC") == BASE + "#/data/databases/DB/schemas/SC"


@pytest.mark.parametrize(
    ("object_type", "segment"),
    [
        (ObjectType.TABLE, "table"),
        (ObjectType.VIEW, "view"),
        (ObjectType.PIPE, "pipe"),
        (ObjectType.STAGE, "stage"),
        (ObjectType.TASK, "task"),
    ],
)
def test_every_object_type_has_its_snowsight_route(
    object_type: ObjectType, segment: str
) -> None:
    ref = ObjectRef(object_type, "DB", "SC", "NAME")
    assert object_url(BASE, ref) == (
        f"{BASE}#/data/databases/DB/schemas/SC/{segment}/NAME"
    )


def test_a_name_keeps_its_case_and_is_percent_encoded() -> None:
    """As Snowsight writes a quoted identifier: `probe lower` is
    `probe%20lower`, and a `/` must not split the route."""
    ref = ObjectRef(ObjectType.TABLE, "DB", "SC", "probe lower/x")
    assert object_url(BASE, ref).endswith("/table/probe%20lower%2Fx")
