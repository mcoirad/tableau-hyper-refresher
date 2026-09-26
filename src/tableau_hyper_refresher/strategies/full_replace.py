"""Full-replace strategy implementation."""

import logging
from typing import Any

from tableauhyperapi import Connection, TableName

from ..redshift import RedshiftClient
from .base import RefreshStrategy, StrategyContext

logger = logging.getLogger(__name__)


class FullReplaceStrategy(RefreshStrategy):
    """Drop/recreate when needed, then truncate/copy files."""

    def _replace_table_ddl(
        self,
        con: Connection,
        tname: TableName,
        table_details: dict[str, Any],
    ) -> bool:
        redshift_client = RedshiftClient()
        table_definition = con.catalog.get_table_definition(tname)
        logger.info(f"Table definition: {table_definition}")
        hyper_col_list = [col_def.name for col_def in table_definition.columns]
        redshift_col_list = table_details.get("column_names")
        if redshift_col_list is None:
            raise RuntimeError(f"No column_names found for {tname.name.unescaped}")
        do_columns_match = len(hyper_col_list) == len(redshift_col_list)

        if do_columns_match:
            logger.info(
                f"Columns match for {tname.name.unescaped}; recreating the table to preserve full-replacement semantics"
            )
        else:
            logger.info(
                f"Columns don't match for {tname.name.unescaped}, recreating table"
            )

        def _run_ddl() -> None:
            con.execute_command(f"DROP TABLE {tname};")
            table_id = table_details.get("table_id")
            if not table_id or "." not in table_id:
                raise RuntimeError(
                    f"Invalid table_id for {tname.name.unescaped}: {table_id}"
                )
            table_schema, table_name = table_id.split(".", 1)
            create_table_statement = redshift_client.create_table_statement(
                table_schema,
                table_name,
                tname.schema_name,
                tname.name,
            )
            con.execute_command(create_table_statement)

        self.run_in_transaction(con, _run_ddl)
        logger.info(f"Successfully recreated table {tname.name.unescaped}")
        return True

    def _replace_table_dml(
        self,
        con: Connection,
        tname: TableName,
        files: list[str],
        file_format: str,
    ) -> None:
        if not files:
            logger.info(
                f"WARNING: no files provided for {tname.name.unescaped}; leaving it empty."
            )
            return

        table_sql = self.table_sql_name(tname)

        def _run_dml() -> None:
            con.execute_command(f"TRUNCATE TABLE {table_sql}")
            self.copy_files_to_table(con, table_sql, files, file_format)

        self.run_in_transaction(con, _run_dml)
        logger.info(f"Successfully loaded data into {tname.name.unescaped}")

        row_ct = list(con.execute_query(f"SELECT COUNT(*) FROM {table_sql}"))[0][0]
        logger.info(f"Loaded {row_ct:,} rows into {tname.name.unescaped}")

    def apply(self, con: Connection, ctx: StrategyContext) -> None:
        logger.info(
            f"Replacing table {ctx.table_name} in schema {ctx.schema_name} with details: {ctx.table_details}",
        )
        extract_table_name = self.require_extract_table_name(ctx)
        tnames = self.find_table_names(con, ctx.schema_name, extract_table_name)
        for tname in tnames:
            self._replace_table_ddl(con, tname, ctx.table_details)
            self._replace_table_dml(con, tname, ctx.source_files, ctx.file_format)
