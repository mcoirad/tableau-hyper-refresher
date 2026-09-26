from pathlib import Path

import pytest

from tableau_hyper_refresher.refresh_config import RefreshMode, load_refresh_config


def test_loads_defaults_and_table_override(tmp_path: Path) -> None:
    config_file = tmp_path / "refresh.yaml"
    config_file.write_text(
        """
version: 1
defaults:
  mode: full_replace
tables:
  analytics.events:
    mode: upsert
    primary_key: [event_id]
    sort_column: updated_at
    incremental:
      enabled: true
      lookback_minutes: 90
"""
    )

    config = load_refresh_config(str(config_file))
    assert config.for_table("analytics.other").mode == "full_replace"
    table = config.for_table("analytics.events")
    assert table.mode == "upsert"
    assert table.primary_key == ["event_id"]
    assert table.incremental.enabled is True
    assert table.incremental.lookback_minutes == 90


@pytest.mark.parametrize("mode", ["upsert", "merge"])
def test_incremental_modes_require_primary_key_and_sort_column(
    tmp_path: Path, mode: RefreshMode
) -> None:
    config_file = tmp_path / "invalid.yaml"
    config_file.write_text(
        f"version: 1\ntables:\n  analytics.events:\n    mode: {mode}\n"
    )

    with pytest.raises(ValueError, match="primary_key"):
        load_refresh_config(str(config_file))


def test_merge_delete_mode_requires_column(tmp_path: Path) -> None:
    config_file = tmp_path / "invalid.yaml"
    config_file.write_text(
        """
version: 1
tables:
  analytics.events:
    mode: merge
    primary_key: [event_id]
    sort_column: updated_at
    delete:
      mode: deleted_at
"""
    )

    with pytest.raises(ValueError, match="delete.column"):
        load_refresh_config(str(config_file))
