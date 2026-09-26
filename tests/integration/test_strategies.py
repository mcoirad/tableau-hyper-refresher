from pathlib import Path

import pytest
from tableauhyperapi import (
    Connection,
    CreateMode,
    HyperProcess,
    SqlType,
    TableDefinition,
    TableName,
    Telemetry,
)

from tableau_hyper_refresher.refresh_config import (
    DeleteConfig,
    IncrementalConfig,
    TableRefreshConfig,
)
from tableau_hyper_refresher.strategies.base import StrategyContext
from tableau_hyper_refresher.strategies.full_replace import FullReplaceStrategy
from tableau_hyper_refresher.strategies.merge import MergeStrategy
from tableau_hyper_refresher.strategies.upsert import UpsertStrategy

pytestmark = pytest.mark.integration


@pytest.fixture
def hyper_connection(tmp_path: Path):
    database = tmp_path / "fixture.hyper"
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as process:
        with Connection(
            process.endpoint,
            database=str(database),
            create_mode=CreateMode.CREATE_AND_REPLACE,
        ) as connection:
            connection.catalog.create_schema("Extract")
            definition = TableDefinition(
                TableName("Extract", "events"),
                [
                    TableDefinition.Column("tenant_id", SqlType.big_int()),
                    TableDefinition.Column("event_id", SqlType.big_int()),
                    TableDefinition.Column("value", SqlType.text()),
                    TableDefinition.Column("updated_at", SqlType.timestamp()),
                    TableDefinition.Column("is_deleted", SqlType.bool()),
                ],
            )
            connection.catalog.create_table(definition)
            connection.execute_command(
                """
                INSERT INTO "Extract"."events"
                VALUES (1, 1, 'old', TIMESTAMP '2026-01-01 00:00:00', FALSE),
                       (1, 2, 'keep', TIMESTAMP '2026-01-01 00:00:00', FALSE)
                """
            )
            yield connection


def _write_csv(path: Path, rows: list[str]) -> str:
    path.write_text(
        "tenant_id,event_id,value,updated_at,is_deleted\n" + "\n".join(rows) + "\n"
    )
    return str(path)


def _rows(connection: Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in connection.execute_list_query(
            'SELECT tenant_id,event_id,value FROM "Extract"."events" ORDER BY event_id'
        )
    ]


def test_upsert_deduplicates_composite_keys_and_is_idempotent(
    hyper_connection: Connection, tmp_path: Path
) -> None:
    source = _write_csv(
        tmp_path / "upsert.csv",
        [
            "1,1,intermediate,2026-01-02 00:00:00,false",
            "1,1,newest,2026-01-03 00:00:00,false",
            "1,3,inserted,2026-01-03 00:00:00,false",
        ],
    )
    config = TableRefreshConfig(
        table_id="analytics.events",
        mode="upsert",
        primary_key=["tenant_id", "event_id"],
        sort_column="updated_at",
        incremental=IncrementalConfig(enabled=True),
    )
    context = StrategyContext(
        schema_name="Extract",
        table_name="events",
        source_files=[source],
        file_format="csv",
        table_config=config,
        table_details={"extract_table_name": "[Extract].[events]"},
    )

    strategy = UpsertStrategy()
    strategy.apply(hyper_connection, context)
    strategy.apply(hyper_connection, context)

    assert _rows(hyper_connection) == [
        (1, 1, "newest"),
        (1, 2, "keep"),
        (1, 3, "inserted"),
    ]


def test_merge_applies_boolean_deletes(
    hyper_connection: Connection, tmp_path: Path
) -> None:
    source = _write_csv(
        tmp_path / "merge.csv",
        [
            "1,1,removed,2026-01-03 00:00:00,true",
            "1,3,inserted,2026-01-03 00:00:00,false",
        ],
    )
    config = TableRefreshConfig(
        table_id="analytics.events",
        mode="merge",
        primary_key=["tenant_id", "event_id"],
        sort_column="updated_at",
        incremental=IncrementalConfig(enabled=True),
        delete=DeleteConfig(mode="is_deleted", column="is_deleted"),
    )
    context = StrategyContext(
        schema_name="Extract",
        table_name="events",
        source_files=[source],
        file_format="csv",
        table_config=config,
        table_details={"extract_table_name": "[Extract].[events]"},
    )

    MergeStrategy().apply(hyper_connection, context)

    assert _rows(hyper_connection) == [(1, 2, "keep"), (1, 3, "inserted")]


def test_upsert_with_no_files_is_a_noop(
    hyper_connection: Connection,
) -> None:
    config = TableRefreshConfig(
        table_id="analytics.events",
        mode="upsert",
        primary_key=["event_id"],
        sort_column="updated_at",
    )
    UpsertStrategy().apply(
        hyper_connection,
        StrategyContext(
            schema_name="Extract",
            table_name="events",
            source_files=[],
            file_format="csv",
            table_config=config,
            table_details={"extract_table_name": "[Extract].[events]"},
        ),
    )
    assert _rows(hyper_connection) == [(1, 1, "old"), (1, 2, "keep")]


def test_upsert_accepts_multiple_csv_parts_nulls_and_quoted_values(
    hyper_connection: Connection, tmp_path: Path
) -> None:
    first = _write_csv(
        tmp_path / "part-1.csv",
        ['1,3,"value, with comma",2026-01-03 00:00:00,false'],
    )
    second = _write_csv(
        tmp_path / "part-2.csv",
        ["1,4,,2026-01-04 00:00:00,false"],
    )
    config = TableRefreshConfig(
        table_id="analytics.events",
        mode="upsert",
        primary_key=["tenant_id", "event_id"],
        sort_column="updated_at",
    )

    UpsertStrategy().apply(
        hyper_connection,
        StrategyContext(
            schema_name="Extract",
            table_name="events",
            source_files=[first, second],
            file_format="csv",
            table_config=config,
            table_details={"extract_table_name": "[Extract].[events]"},
        ),
    )

    rows = _rows(hyper_connection)
    assert (1, 3, "value, with comma") in rows
    assert (1, 4, None) in rows


def test_merge_applies_deleted_at_removals(
    hyper_connection: Connection, tmp_path: Path
) -> None:
    hyper_connection.execute_command(
        'CREATE TABLE "Extract"."events_deleted" ('
        '"tenant_id" BIGINT, "event_id" BIGINT, "value" TEXT, '
        '"updated_at" TIMESTAMP, "deleted_at" TIMESTAMP)'
    )
    hyper_connection.execute_command(
        'INSERT INTO "Extract"."events_deleted" VALUES '
        "(1, 1, 'remove', TIMESTAMP '2026-01-01 00:00:00', NULL), "
        "(1, 2, 'keep', TIMESTAMP '2026-01-01 00:00:00', NULL)"
    )
    source = tmp_path / "deleted-at.csv"
    source.write_text(
        "tenant_id,event_id,value,updated_at,deleted_at\n"
        "1,1,remove,2026-01-03 00:00:00,2026-01-03 01:00:00\n"
        "1,3,inserted,2026-01-03 00:00:00,\n"
    )
    config = TableRefreshConfig(
        table_id="analytics.events_deleted",
        mode="merge",
        primary_key=["tenant_id", "event_id"],
        sort_column="updated_at",
        delete=DeleteConfig(mode="deleted_at", column="deleted_at"),
    )

    MergeStrategy().apply(
        hyper_connection,
        StrategyContext(
            schema_name="Extract",
            table_name="events_deleted",
            source_files=[str(source)],
            file_format="csv",
            table_config=config,
            table_details={"extract_table_name": "[Extract].[events_deleted]"},
        ),
    )

    rows = hyper_connection.execute_list_query(
        'SELECT event_id, value FROM "Extract"."events_deleted" ORDER BY event_id'
    )
    assert [tuple(row) for row in rows] == [(2, "keep"), (3, "inserted")]


def test_full_replace_recreates_schema_and_loads_multiple_parts(
    hyper_connection: Connection, tmp_path: Path, monkeypatch
) -> None:
    hyper_connection.execute_command(
        'CREATE TABLE "Extract"."replace_me" ("old_column" TEXT)'
    )
    first = tmp_path / "replace-1.csv"
    first.write_text('id,label,amount\n1,"quoted, value",1.5\n')
    second = tmp_path / "replace-2.csv"
    second.write_text("id,label,amount\n2,,\n")

    class FakeRedshiftClient:
        def create_table_statement(self, *args, **kwargs) -> str:
            return (
                'CREATE TABLE "Extract"."replace_me" ('
                '"id" BIGINT NOT NULL, "label" TEXT, "amount" DOUBLE PRECISION)'
            )

    monkeypatch.setattr(
        "tableau_hyper_refresher.strategies.full_replace.RedshiftClient",
        FakeRedshiftClient,
    )
    FullReplaceStrategy().apply(
        hyper_connection,
        StrategyContext(
            schema_name="Extract",
            table_name="replace_me",
            source_files=[str(first), str(second)],
            file_format="csv",
            table_config=TableRefreshConfig(
                table_id="analytics.replace_me", mode="full_replace"
            ),
            table_details={
                "extract_table_name": "[Extract].[replace_me]",
                "table_id": "analytics.replace_me",
                "column_names": ["id", "label", "amount"],
            },
        ),
    )

    columns = hyper_connection.catalog.get_table_definition(
        TableName("Extract", "replace_me")
    ).columns
    assert [column.name.unescaped for column in columns] == ["id", "label", "amount"]
    rows = hyper_connection.execute_list_query(
        'SELECT id, label, amount FROM "Extract"."replace_me" ORDER BY id'
    )
    assert [tuple(row) for row in rows] == [
        (1, "quoted, value", 1.5),
        (2, None, None),
    ]
