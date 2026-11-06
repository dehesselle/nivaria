# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""SHOW COLUMNS -> INFORMATION_SCHEMA.COLUMNS mapping.

The account nivaria was verified against runs with no warehouse, and
INFORMATION_SCHEMA is an ordinary SELECT that needs one. SHOW COLUMNS is
served by the metadata layer instead, but returns a different shape, so the
mapping below is what keeps the cache mirror stable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nivaria.snowflake.queries import map_show_columns, show_columns

#: SHOW COLUMNS' data_type JSON next to what DESC TABLE printed for the same
#: column, captured from a live account -- one column per declared type.
_LIVE_TYPES = json.loads(
    (Path(__file__).parent / "data" / "show_column_types.json").read_text(
        encoding="utf-8"
    )
)


def raw(**overrides) -> dict:
    row = {
        "table_name": "ORDERS",
        "schema_name": "PUBLIC",
        "database_name": "MYDB",
        "column_name": "ID",
        "data_type": '{"type":"FIXED","precision":38,"scale":0,"nullable":false}',
        "null?": "N",
        "default": None,
        "kind": "COLUMN",
        "expression": None,
        "comment": "the key",
        "autoincrement": "",
    }
    row.update(overrides)
    return row


def test_the_statement_is_fully_qualified_and_quoted() -> None:
    assert show_columns("MYDB", "PUBLIC", "ORDERS") == (
        'SHOW COLUMNS IN TABLE "MYDB"."PUBLIC"."ORDERS"'
    )


def test_identifiers_are_quoted_so_case_is_preserved() -> None:
    assert show_columns("my_db", "my_schema", "my_table") == (
        'SHOW COLUMNS IN TABLE "my_db"."my_schema"."my_table"'
    )


def test_names_map_onto_information_schema_vocabulary() -> None:
    mapped = map_show_columns([raw()])[0]
    assert mapped["TABLE_CATALOG"] == "MYDB"
    assert mapped["TABLE_SCHEMA"] == "PUBLIC"
    assert mapped["TABLE_NAME"] == "ORDERS"
    assert mapped["COLUMN_NAME"] == "ID"
    assert mapped["COMMENT"] == "the key"


def test_the_data_type_json_is_unpacked() -> None:
    """SHOW COLUMNS returns data_type as a JSON blob, not a type name."""
    mapped = map_show_columns([raw()])[0]
    assert mapped["DATA_TYPE"] == "NUMBER"
    assert mapped["NUMERIC_PRECISION"] == 38
    assert mapped["NUMERIC_SCALE"] == 0


def test_shows_internal_type_names_are_stored_as_information_schemas() -> None:
    """`FIXED` and `REAL` are SHOW COLUMNS' own categories -- `FIXED` is not
    even valid DDL. INFORMATION_SCHEMA calls them NUMBER and FLOAT, and the
    cache mirrors INFORMATION_SCHEMA's values as well as its field names."""
    fixed = '{"type":"FIXED","precision":10,"scale":2,"nullable":true}'
    real = '{"type":"REAL","nullable":true}'
    mapped = map_show_columns([raw(data_type=fixed), raw(data_type=real)])
    assert [m["DATA_TYPE"] for m in mapped] == ["NUMBER", "FLOAT"]
    assert (mapped[0]["NUMERIC_PRECISION"], mapped[0]["NUMERIC_SCALE"]) == (10, 2)
    assert mapped[1]["NUMERIC_PRECISION"] is None


def test_text_length_lands_in_character_maximum_length() -> None:
    mapped = map_show_columns(
        [raw(data_type='{"type":"TEXT","length":16777216,"nullable":true}')]
    )[0]
    assert mapped["DATA_TYPE"] == "TEXT"
    assert mapped["CHARACTER_MAXIMUM_LENGTH"] == 16777216
    assert mapped["NUMERIC_PRECISION"] is None


def test_timestamp_scale_lands_in_datetime_precision_not_numeric_scale() -> None:
    """TIMESTAMP_LTZ's JSON reuses the "precision"/"scale" keys NUMBER uses for
    its decimal scale, but INFORMATION_SCHEMA keeps the two concepts apart."""
    mapped = map_show_columns(
        [
            raw(
                data_type='{"type":"TIMESTAMP_LTZ","precision":0,"scale":9,'
                '"nullable":true}'
            )
        ]
    )[0]
    assert mapped["DATA_TYPE"] == "TIMESTAMP_LTZ"
    assert mapped["DATETIME_PRECISION"] == 9
    assert mapped["NUMERIC_PRECISION"] is None
    assert mapped["NUMERIC_SCALE"] is None


def test_nullability_comes_from_the_null_question_mark_column() -> None:
    assert map_show_columns([raw(**{"null?": "Y"})])[0]["IS_NULLABLE"] == "YES"
    assert map_show_columns([raw(**{"null?": "N"})])[0]["IS_NULLABLE"] == "NO"


def test_nullability_falls_back_to_the_json_payload() -> None:
    """Older or trimmed results may omit the column; the JSON carries it too."""
    row = raw(data_type='{"type":"TEXT","nullable":true}')
    del row["null?"]
    assert map_show_columns([row])[0]["IS_NULLABLE"] == "YES"


def test_position_is_derived_from_declaration_order() -> None:
    """SHOW COLUMNS has no ORDINAL_POSITION; it returns columns in order."""
    rows = [raw(column_name=name) for name in ("ID", "TOTAL", "CREATED")]
    mapped = map_show_columns(rows)
    assert [m["ORDINAL_POSITION"] for m in mapped] == [1, 2, 3]
    assert [m["COLUMN_NAME"] for m in mapped] == ["ID", "TOTAL", "CREATED"]


def test_position_restarts_for_each_table() -> None:
    """A multi-table result must not keep incrementing across tables, or the
    second table's columns would sort after the first's forever."""
    rows = [
        raw(table_name="A", column_name="A1"),
        raw(table_name="A", column_name="A2"),
        raw(table_name="B", column_name="B1"),
    ]
    mapped = map_show_columns(rows)
    assert [m["ORDINAL_POSITION"] for m in mapped] == [1, 2, 1]


def test_an_unparseable_data_type_degrades_rather_than_fails() -> None:
    """A column nivaria cannot describe is still a column worth listing."""
    mapped = map_show_columns([raw(data_type="not json at all")])[0]
    assert mapped["DATA_TYPE"] == "UNKNOWN"
    assert mapped["COLUMN_NAME"] == "ID"


def test_a_missing_data_type_degrades_rather_than_fails() -> None:
    mapped = map_show_columns([raw(data_type=None)])[0]
    assert mapped["DATA_TYPE"] == "UNKNOWN"


def test_an_empty_result_maps_to_nothing() -> None:
    assert map_show_columns([]) == []


@pytest.mark.parametrize(
    "case", _LIVE_TYPES, ids=[case["declared"] for case in _LIVE_TYPES]
)
def test_the_desc_type_matches_what_desc_table_printed_live(case) -> None:
    """The column list shows `_desc_type`, so it has to be Snowflake's own
    rendering to the character -- INT as NUMBER(38,0), CHAR as VARCHAR(1),
    a VECTOR's element as declared, NOT NULL only on an object's fields."""
    mapped = map_show_columns([raw(data_type=case["show_data_type"])])[0]
    assert mapped["_desc_type"] == case["desc_type"]


def test_a_binary_length_is_kept_out_of_character_maximum_length() -> None:
    """INFORMATION_SCHEMA leaves every length field NULL for BINARY; the
    length still reaches the column list, through `_desc_type`."""
    mapped = map_show_columns(
        [raw(data_type='{"type":"BINARY","length":8,"byteLength":8}')]
    )[0]
    assert mapped["CHARACTER_MAXIMUM_LENGTH"] is None
    assert mapped["_desc_type"] == "BINARY(8)"


def test_an_unparseable_data_type_renders_as_unknown() -> None:
    assert map_show_columns([raw(data_type="not json")])[0]["_desc_type"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"type":"FIXED","precision":38}', "NUMBER"),
        ('{"type":"ARRAY","elementType":"oops"}', "ARRAY"),
        ('{"type":"MAP","keyType":{"type":"TEXT"}}', "MAP"),
        ('{"type":"OBJECT","fields":[{"fieldName":"a"}]}', "OBJECT"),
        (
            '{"type":"VECTOR","vectorElementType":{"type":"TEXT"},"dimension":3}',
            "VECTOR",
        ),
        ('{"type":"SOMETHING_NEW","precision":5}', "SOMETHING_NEW"),
    ],
)
def test_a_malformed_or_unknown_type_degrades_to_its_bare_name(
    payload, expected
) -> None:
    """A partial or unexpected payload renders the category alone rather than
    a malformed type -- or failing the whole fetch."""
    assert map_show_columns([raw(data_type=payload)])[0]["_desc_type"] == expected
