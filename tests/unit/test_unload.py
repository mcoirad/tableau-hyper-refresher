from tableau_hyper_refresher.refresh_config import (
    IncrementalConfig,
    TableRefreshConfig,
)
from tableau_hyper_refresher.unload import (
    build_table_select_sql,
    parse_datasource_ids,
    resolve_effective_last_applied_sort_value,
)


def _incremental_config() -> TableRefreshConfig:
    return TableRefreshConfig(
        table_id="analytics.events",
        mode="upsert",
        primary_key=["event_id"],
        sort_column="updated_at",
        incremental=IncrementalConfig(enabled=True, lookback_minutes=60),
    )


def test_incremental_select_uses_lookback() -> None:
    sql = build_table_select_sql(
        "analytics",
        "events",
        _incremental_config(),
        "2026-01-02T03:00:00Z",
        False,
    )
    assert sql == (
        "SELECT * FROM analytics.events WHERE updated_at >= '2026-01-02T02:00:00Z'"
    )


def test_force_full_load_ignores_watermark() -> None:
    assert (
        build_table_select_sql(
            "analytics",
            "events",
            _incremental_config(),
            "2026-01-02T03:00:00Z",
            True,
        )
        == "SELECT * FROM analytics.events"
    )


def test_datasource_ids_are_validated_and_deduplicated() -> None:
    assert parse_datasource_ids("abcd1234, ABCD1234,abcd1234") == [
        "ABCD1234",
        "abcd1234",
    ]


class _State:
    def __init__(self, values: dict[str, str | None]):
        self.values = values

    def get_tableau_datasource_table(self, datasource_id: str, table_key: str):
        del table_key
        value = self.values[datasource_id]
        return {"last_applied_sort_value": value} if value else None


def test_shared_table_uses_earliest_watermark() -> None:
    state = _State(
        {
            "aaaaaaaa": "2026-01-03T00:00:00Z",
            "bbbbbbbb": "2026-01-02T00:00:00Z",
        }
    )
    assert (
        resolve_effective_last_applied_sort_value(
            state,  # type: ignore[arg-type]
            "analytics.events",
            "2026-01-04T00:00:00Z",
            ["aaaaaaaa", "bbbbbbbb"],
        )
        == "2026-01-02T00:00:00Z"
    )


def test_missing_datasource_watermark_forces_catch_up() -> None:
    state = _State({"aaaaaaaa": None})
    assert (
        resolve_effective_last_applied_sort_value(
            state,  # type: ignore[arg-type]
            "analytics.events",
            "2026-01-04T00:00:00Z",
            ["aaaaaaaa"],
        )
        is None
    )
