"""Merge strategy implementation."""

import logging
import uuid

from tableauhyperapi import Connection, TableName

from .base import RefreshStrategy, StrategyContext

logger = logging.getLogger(__name__)


def _insert_filter(
    strategy: RefreshStrategy, delete_mode: str, delete_col: str | None
) -> str:
    if delete_mode == "is_deleted":
        if not delete_col:
            raise ValueError(
                "delete.column is required for merge with delete.mode='is_deleted'"
            )
        # Keep only rows where marker is false/null
        return (
            f"(l.{strategy.quote_ident(delete_col)} IS NULL OR "
            f"l.{strategy.quote_ident(delete_col)} = FALSE)"
        )
    if delete_mode == "deleted_at":
        if not delete_col:
            raise ValueError(
                "delete.column is required for merge with delete.mode='deleted_at'"
            )
        # Keep only rows where deleted_at is null
        return f"l.{strategy.quote_ident(delete_col)} IS NULL"

    # delete.mode='none' behaves like upsert.
    return "TRUE"


class MergeStrategy(RefreshStrategy):
    """Upsert plus delete semantics via is_deleted/deleted_at."""

    def apply(self, con: Connection, ctx: StrategyContext) -> None:
        pk_cols = list(ctx.table_config.primary_key)
        sort_col = ctx.table_config.sort_column
        if not pk_cols:
            raise ValueError(f"{ctx.table_name}: primary_key is required for merge")
        if not sort_col:
            raise ValueError(f"{ctx.table_name}: sort_column is required for merge")
        if not ctx.source_files:
            logger.info(f"No source files for {ctx.table_name}; skipping merge")
            return

        delete_mode = ctx.table_config.delete.mode
        delete_col = ctx.table_config.delete.column
        insert_predicate = _insert_filter(self, delete_mode, delete_col)
        extract_table_name = self.require_extract_table_name(ctx)
        target_tables = self.find_table_names(con, ctx.schema_name, extract_table_name)

        for target_tname in target_tables:
            target_sql = self.table_sql_name(target_tname)
            target_def = con.catalog.get_table_definition(target_tname)
            target_cols = [col.name.unescaped for col in target_def.columns]
            logger.info(f"Target cols: {target_cols}")

            missing_pk = [c for c in pk_cols if c not in target_cols]
            if missing_pk:
                raise ValueError(
                    f"{ctx.table_name}: primary_key columns missing in target table: {missing_pk}"
                )
            if sort_col not in target_cols:
                raise ValueError(
                    f"{ctx.table_name}: sort_column '{sort_col}' missing in target table"
                )
            if delete_col and delete_col not in target_cols:
                raise ValueError(
                    f"{ctx.table_name}: delete column '{delete_col}' missing in target table"
                )

            run_suffix = uuid.uuid4().hex[:12]
            staging_tname = TableName(
                target_tname.schema_name,
                f"{target_tname.name.unescaped}__stg_{run_suffix}",
            )
            latest_tname = TableName(
                target_tname.schema_name,
                f"{target_tname.name.unescaped}__latest_{run_suffix}",
            )
            staging_sql = self.table_sql_name(staging_tname)
            latest_sql = self.table_sql_name(latest_tname)

            cols_sql = ", ".join(self.quote_ident(c) for c in target_cols)
            pk_partition_sql = ", ".join(self.quote_ident(c) for c in pk_cols)
            pk_not_null_sql = " AND ".join(
                f"{self.quote_ident(c)} IS NOT NULL" for c in pk_cols
            )
            latest_select_cols_sql = ", ".join(
                f"l.{self.quote_ident(c)}" for c in target_cols
            )
            pk_join_sql = " AND ".join(
                f"t.{self.quote_ident(c)} = l.{self.quote_ident(c)}" for c in pk_cols
            )

            logger.info(
                f"Merge table {target_tname.name.unescaped} with PK={pk_cols} sort={sort_col} delete_mode={delete_mode}"
            )

            # DDL transaction: create temp tables
            def _create_temp_tables() -> None:
                con.execute_command(
                    f"CREATE TABLE {staging_sql} AS SELECT * FROM {target_sql} WHERE 1=0"
                )
                con.execute_command(
                    f"CREATE TABLE {latest_sql} AS SELECT * FROM {target_sql} WHERE 1=0"
                )

            self.run_in_transaction(con, _create_temp_tables)

            # DML transaction: copy, dedupe, delete-matching-keys, insert surviving rows
            def _run_merge() -> None:
                self.copy_files_to_table(
                    con, staging_sql, ctx.source_files, ctx.file_format
                )

                con.execute_command(
                    f"""
                    INSERT INTO {latest_sql} ({cols_sql})
                    SELECT {cols_sql}
                    FROM (
                        SELECT
                            {cols_sql},
                            ROW_NUMBER() OVER (
                                PARTITION BY {pk_partition_sql}
                                ORDER BY {self.quote_ident(sort_col)} DESC
                            ) AS _rn
                        FROM {staging_sql}
                        WHERE {pk_not_null_sql}
                    ) d
                    WHERE d._rn = 1
                    """
                )

                con.execute_command(
                    f"""
                    DELETE FROM {target_sql} t
                    WHERE EXISTS (
                        SELECT 1
                        FROM {latest_sql} l
                        WHERE {pk_join_sql}
                    )
                    """
                )

                con.execute_command(
                    f"""
                    INSERT INTO {target_sql} ({cols_sql})
                    SELECT {latest_select_cols_sql}
                    FROM {latest_sql} l
                    WHERE {insert_predicate}
                    """
                )

            try:
                self.run_in_transaction(con, _run_merge)
            finally:
                # Best-effort cleanup in separate DDL transaction.
                def _drop_temp_tables() -> None:
                    con.execute_command(f"DROP TABLE {latest_sql}")
                    con.execute_command(f"DROP TABLE {staging_sql}")

                try:
                    self.run_in_transaction(con, _drop_temp_tables)
                except Exception:
                    logger.warning(
                        "Failed to cleanup temp tables for %s",
                        target_tname.name.unescaped,
                    )
