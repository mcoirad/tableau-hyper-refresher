"""Base strategy interfaces and shared helpers for Hyper table writes."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from tableauhyperapi import Connection, TableName
from tableauhyperapi.sql import escape_string_literal

from ..refresh_config import TableRefreshConfig


@dataclass(frozen=True)
class StrategyContext:
    schema_name: str
    table_name: str
    source_files: list[str]
    file_format: str
    table_config: TableRefreshConfig
    table_details: dict[str, Any]


class RefreshStrategy(ABC):
    """Strategy for writing table data into a Hyper table."""

    COPY_OPTS = {
        "csv": "WITH (format csv, header true)",
        "parquet": "WITH (format parquet)",
    }

    @property
    def logger(self) -> logging.Logger:
        # Uses the concrete strategy module/class logger namespace.
        return logging.getLogger(self.__class__.__module__)

    @abstractmethod
    def apply(self, con: Connection, ctx: StrategyContext) -> None:
        """Apply table write behavior for one table."""
        raise NotImplementedError

    @classmethod
    def copy_options(cls, file_format: str) -> str:
        try:
            return cls.COPY_OPTS[file_format]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported file format for COPY: {file_format}"
            ) from exc

    @staticmethod
    def quote_ident(name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    @classmethod
    def table_sql_name(cls, tname: TableName) -> str:
        schema = cls._schema_name_to_str(tname.schema_name)
        table = cls._name_to_str(tname.name)
        return f"{cls.quote_ident(schema)}.{cls.quote_ident(table)}"

    @staticmethod
    def clean_hyper_table_name(hyper_table_name: str) -> str:
        return hyper_table_name.replace("[", "").replace("]", "").split(".")[-1]

    @classmethod
    def find_table_names(
        cls,
        con: Connection,
        schema: str,
        replacing_table: str,
    ) -> list[TableName]:
        table_names = con.catalog.get_table_names(schema)
        clean_name = cls.clean_hyper_table_name(replacing_table)
        matches = [
            tname for tname in table_names if cls._name_to_str(tname.name) == clean_name
        ]

        if not matches:
            raise RuntimeError(f"No tables matched for {replacing_table}")
        if len(matches) > 1:
            logging.getLogger(__name__).info(
                "WARN: Multiple tables matched for %s: %s", replacing_table, matches
            )
        return matches

    @staticmethod
    def require_extract_table_name(ctx: StrategyContext) -> str:
        extract_table_name = ctx.table_details.get("extract_table_name")
        if not extract_table_name:
            raise RuntimeError(
                f"No extract_table_name provided for table {ctx.table_name}"
            )
        return str(extract_table_name)

    @classmethod
    def run_in_transaction(cls, con: Connection, fn: Callable[[], None]) -> None:
        con.execute_command("BEGIN")
        try:
            fn()
            con.execute_command("COMMIT")
        except Exception:
            con.execute_command("ROLLBACK")
            raise

    @classmethod
    def copy_files_to_table(
        cls,
        con: Connection,
        table_sql_name: str,
        files: Iterable[str],
        file_format: str,
    ) -> None:
        copy_opts = cls.copy_options(file_format)
        for path in files:
            con.execute_command(
                f"COPY {table_sql_name} FROM {escape_string_literal(os.path.abspath(path))} {copy_opts};"
            )

    @staticmethod
    def _name_to_str(value: Any) -> str:
        unescaped = getattr(value, "unescaped", None)
        if isinstance(unescaped, str):
            return unescaped
        return str(value)

    @classmethod
    def _schema_name_to_str(cls, value: Any) -> str:
        # Handles both SchemaName-like objects and plain strings.
        name_attr = getattr(value, "name", None)
        if name_attr is not None:
            return cls._name_to_str(name_attr)
        return cls._name_to_str(value)
