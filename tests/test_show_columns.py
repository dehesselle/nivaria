"""SHOW COLUMNS -> INFORMATION_SCHEMA.COLUMNS mapping.

The account snowcone was verified against runs with no warehouse, and
INFORMATION_SCHEMA is an ordinary SELECT that needs one. SHOW COLUMNS is
served by the metadata layer instead, but returns a different shape, so the
mapping below is what keeps the cache mirror stable.
"""

from __future__ import annotations

from snowcone.gateway.queries import map_show_columns, show_columns


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
    assert mapped["DATA_TYPE"] == "FIXED"
    assert mapped["NUMERIC_PRECISION"] == 38
    assert mapped["NUMERIC_SCALE"] == 0


def test_text_length_lands_in_character_maximum_length() -> None:
    mapped = map_show_columns(
        [raw(data_type='{"type":"TEXT","length":16777216,"nullable":true}')]
    )[0]
    assert mapped["DATA_TYPE"] == "TEXT"
    assert mapped["CHARACTER_MAXIMUM_LENGTH"] == 16777216
    assert mapped["NUMERIC_PRECISION"] is None


def test_timestamp_scale_lands_in_datetime_precision_not_numeric_scale() -> None:
    """TIMESTAMP_LTZ's JSON reuses the "precision"/"scale" keys FIXED uses for
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
    """A column snowcone cannot describe is still a column worth listing."""
    mapped = map_show_columns([raw(data_type="not json at all")])[0]
    assert mapped["DATA_TYPE"] == "UNKNOWN"
    assert mapped["COLUMN_NAME"] == "ID"


def test_a_missing_data_type_degrades_rather_than_fails() -> None:
    mapped = map_show_columns([raw(data_type=None)])[0]
    assert mapped["DATA_TYPE"] == "UNKNOWN"


def test_an_empty_result_maps_to_nothing() -> None:
    assert map_show_columns([]) == []
