"""Config models and loaders for Hyper refresh strategies.

This module is scaffolding only and does not alter current refresh behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import yaml

from .state import DynamoDBClient

RefreshMode = Literal["full_replace", "upsert", "merge"]
DeleteMode = Literal["none", "is_deleted", "deleted_at"]


@dataclass(frozen=True)
class IncrementalConfig:
    enabled: bool = False
    lookback_minutes: int = 60


@dataclass(frozen=True)
class DeleteConfig:
    mode: DeleteMode = "none"
    column: Optional[str] = None


@dataclass(frozen=True)
class TableRefreshConfig:
    table_id: str
    mode: RefreshMode = "full_replace"
    primary_key: list[str] = field(default_factory=list)
    sort_column: Optional[str] = None
    incremental: IncrementalConfig = field(default_factory=IncrementalConfig)
    delete: DeleteConfig = field(default_factory=DeleteConfig)

    def validate(self) -> None:
        if self.mode in {"upsert", "merge"}:
            if not self.primary_key:
                raise ValueError(
                    f"{self.table_id}: primary_key is required for mode={self.mode}"
                )
            if not self.sort_column:
                raise ValueError(
                    f"{self.table_id}: sort_column is required for mode={self.mode}"
                )

        if (
            self.mode == "merge"
            and self.delete.mode != "none"
            and not self.delete.column
        ):
            raise ValueError(
                f"{self.table_id}: delete.column is required when delete.mode is set"
            )


@dataclass(frozen=True)
class RefreshConfig:
    version: int = 1
    defaults: TableRefreshConfig = field(
        default_factory=lambda: TableRefreshConfig(table_id="defaults")
    )
    tables: Dict[str, TableRefreshConfig] = field(default_factory=dict)

    def for_table(self, table_id: str) -> TableRefreshConfig:
        table_cfg = self.tables.get(table_id)
        if table_cfg is None:
            # Apply defaults with a concrete table ID.
            merged = TableRefreshConfig(
                table_id=table_id,
                mode=self.defaults.mode,
                primary_key=list(self.defaults.primary_key),
                sort_column=self.defaults.sort_column,
                incremental=self.defaults.incremental,
                delete=self.defaults.delete,
            )
            merged.validate()
            return merged

        merged = TableRefreshConfig(
            table_id=table_id,
            mode=table_cfg.mode,
            primary_key=list(table_cfg.primary_key) or list(self.defaults.primary_key),
            sort_column=table_cfg.sort_column or self.defaults.sort_column,
            incremental=table_cfg.incremental,
            delete=table_cfg.delete,
        )
        merged.validate()
        return merged


def _to_incremental(raw: Dict[str, Any]) -> IncrementalConfig:
    return IncrementalConfig(
        enabled=bool(raw.get("enabled", False)),
        lookback_minutes=int(raw.get("lookback_minutes", 60)),
    )


def _to_delete(raw: Dict[str, Any]) -> DeleteConfig:
    mode = raw.get("mode", "none")
    if mode not in {"none", "is_deleted", "deleted_at"}:
        raise ValueError(f"Unsupported delete.mode: {mode}")
    return DeleteConfig(mode=mode, column=raw.get("column"))


def _to_table_config(table_id: str, raw: Dict[str, Any]) -> TableRefreshConfig:
    mode = raw.get("mode", "full_replace")
    if mode not in {"full_replace", "upsert", "merge"}:
        raise ValueError(f"Unsupported mode for {table_id}: {mode}")

    cfg = TableRefreshConfig(
        table_id=table_id,
        mode=mode,
        primary_key=list(raw.get("primary_key", [])),
        sort_column=raw.get("sort_column"),
        incremental=_to_incremental(raw.get("incremental", {})),
        delete=_to_delete(raw.get("delete", {})),
    )
    cfg.validate()
    return cfg


def load_refresh_config(path: str) -> RefreshConfig:
    """Load refresh config from YAML."""
    if yaml is None:
        raise RuntimeError(
            "YAML support requires PyYAML. Install dependency 'pyyaml' in this runtime."
        )
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text()) or {}

    defaults_raw = raw.get("defaults", {})
    tables_raw = raw.get("tables", {})

    defaults = _to_table_config("defaults", defaults_raw)
    tables: Dict[str, TableRefreshConfig] = {}
    for table_id, table_cfg_raw in tables_raw.items():
        tables[table_id] = _to_table_config(table_id, table_cfg_raw)

    return RefreshConfig(
        version=int(raw.get("version", 1)),
        defaults=defaults,
        tables=tables,
    )


@dataclass(frozen=True)
class TableWatermark:
    table_id: str
    last_applied_sort_value: Optional[str] = None


class DynamoTableWatermarkStore:
    """Watermark adapter backed by `tableau_hyper_refresh_tables`.

    This uses existing DynamoDBClient helpers and does not change table schema.
    TODO: needs to be integratd with common.py
    """

    def __init__(self, dynamo_client: Optional[DynamoDBClient] = None):
        self.dynamo = dynamo_client or DynamoDBClient()

    def get(self, table_id: str) -> TableWatermark:
        row = self.dynamo.get_tableau_table(table_id) or {}
        return TableWatermark(
            table_id=table_id,
            last_applied_sort_value=row.get("last_applied_sort_value"),
        )

    def put(self, table_id: str, last_applied_sort_value: str) -> None:
        self.dynamo.update_tableau_table(
            table_id,
            {"last_applied_sort_value": last_applied_sort_value},
        )
