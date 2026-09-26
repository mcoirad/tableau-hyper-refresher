"""Amazon Redshift Data API operations."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import boto3
from tenacity import retry

from .settings import RETRY_KWARGS, ColumnMetadataField, get_env

logger = logging.getLogger(__name__)


class RedshiftClient:
    """Wrapper for Redshift Data API operations."""

    def __init__(self):
        self.aws_region = get_env("AWS_REGION", "us-west-2")
        self.redshift_db = get_env("REDSHIFT_DB")
        self.redshift_workgroup = os.environ.get("REDSHIFT_SERVERLESS_WORKGROUP")
        self.redshift_cluster_id = os.environ.get("REDSHIFT_CLUSTER_ID")
        self.redshift_secret_arn = os.environ.get("REDSHIFT_SECRET_ARN")
        self.redshift_db_user = os.environ.get("REDSHIFT_DB_USER")

        self.rds_data = boto3.client("redshift-data", region_name=self.aws_region)

    def _base_exec_kwargs(self) -> Dict:
        """Build the common ExecuteStatement kwargs for Serverless or Provisioned."""
        kw = {"Database": self.redshift_db}

        if self.redshift_workgroup:
            kw["WorkgroupName"] = self.redshift_workgroup
        elif self.redshift_cluster_id:
            kw["ClusterIdentifier"] = self.redshift_cluster_id
        else:
            raise ValueError(
                "Set either REDSHIFT_SERVERLESS_WORKGROUP or REDSHIFT_CLUSTER_ID"
            )

        if self.redshift_secret_arn:
            kw["SecretArn"] = self.redshift_secret_arn
        elif self.redshift_db_user:
            kw["DbUser"] = self.redshift_db_user
        else:
            raise ValueError(
                "Set REDSHIFT_SECRET_ARN (recommended) or REDSHIFT_DB_USER"
            )

        return kw

    @retry(**RETRY_KWARGS)
    def execute(
        self,
        sql: str,
        with_results: bool = False,
        sleep_sec: float = 1.0,
        timeout_sec: int = 3600,
    ) -> Optional[List[List]]:
        """
        Execute SQL via Redshift Data API. Optionally fetch results (for SELECT/SHOW/etc.).
        Raises RuntimeError on failure/timeouts. Returns list-of-lists for results if requested.
        """
        kwargs = self._base_exec_kwargs()
        resp = self.rds_data.execute_statement(Sql=sql, **kwargs)
        statement_id = resp["Id"]

        # Poll for completion
        start = time.time()
        while True:
            d = self.rds_data.describe_statement(Id=statement_id)
            status = d["Status"]
            if status in ("FINISHED", "FAILED", "ABORTED"):
                break
            if time.time() - start > timeout_sec:
                try:
                    self.rds_data.cancel_statement(Id=statement_id)
                except Exception as e:
                    logger.warning(f"Failed to cancel statement {statement_id}: {e}")
                raise RuntimeError(
                    f"Statement timed out after {timeout_sec}s: {statement_id}"
                )
            time.sleep(sleep_sec)

        if status != "FINISHED":
            raise RuntimeError(f"Statement {status}: {d.get('Error', 'Unknown error')}")

        if not with_results:
            return None

        # Get results (handle pagination)
        rows = []
        get_args = {"Id": statement_id}
        while True:
            res = self.rds_data.get_statement_result(**get_args)
            for rec in res["Records"]:
                row: list[Any] = []
                for cell in rec:
                    if "isNull" in cell and cell["isNull"]:
                        row.append(None)
                    else:
                        row.append(next(iter(cell.values())))
                rows.append(row)
            if "NextToken" in res:
                get_args["NextToken"] = res["NextToken"]
            else:
                break
        return rows

    def _fetch_table_column_metadata(self, schema: str, table: str) -> List[List]:
        """Fetch column metadata for a Redshift table or view from svv_all_columns."""
        q = f"""
            SELECT column_name,
                data_type,
                CAST(NULL AS VARCHAR) AS udt_name,
                UPPER(is_nullable) AS is_nullable,
                numeric_precision,
                numeric_scale,
                character_maximum_length
            FROM svv_all_columns
            WHERE schema_name = '{schema}'
              AND table_name = '{table}'
            ORDER BY ordinal_position;
        """
        rows = self.execute(q, with_results=True) or []
        logger.info(f"Found {len(rows)} columns for {schema}.{table}")
        return rows

    def get_table_columns(self, schema: str, table: str) -> List[List]:
        """Return column metadata with original data types from Redshift using Data API."""
        rows = self._fetch_table_column_metadata(schema, table)
        field_count = ColumnMetadataField.signature_field_count()
        return [row[:field_count] for row in rows]

    def create_table_statement(
        self,
        schema: str,
        table: str,
        target_schema: Optional[str] = None,
        target_table: Optional[str] = None,
    ) -> str:
        """
        Generate a CREATE TABLE statement for the specified Redshift table or view.

        Args:
            schema: Source Redshift schema name
            table: Source Redshift table name
            target_schema: Optional target schema name (defaults to source schema)
            target_table: Optional target table name (defaults to source table)

        Returns:
            CREATE TABLE SQL statement as a string
        """
        target_schema = target_schema or schema
        target_table = target_table or table

        rows = self._fetch_table_column_metadata(schema, table)

        if not rows:
            raise ValueError(f"No columns found for {schema}.{table}")

        column_definitions = []
        for row in rows:
            column_name = row[ColumnMetadataField.COLUMN_NAME]
            data_type = row[ColumnMetadataField.DATA_TYPE]
            udt_name = row[ColumnMetadataField.UDT_NAME]
            is_nullable = row[ColumnMetadataField.IS_NULLABLE]
            numeric_precision = row[ColumnMetadataField.NUMERIC_PRECISION]
            numeric_scale = row[ColumnMetadataField.NUMERIC_SCALE]
            character_maximum_length = (
                row[ColumnMetadataField.CHARACTER_MAXIMUM_LENGTH]
                if len(row) > ColumnMetadataField.CHARACTER_MAXIMUM_LENGTH
                else None
            )

            # Build column definition
            col_parts = [f'"{column_name}"']

            # Build data type string
            type_str = self._format_data_type(
                data_type,
                udt_name,
                numeric_precision,
                numeric_scale,
                character_maximum_length,
            )
            col_parts.append(type_str)

            # Add NULL/NOT NULL constraint
            if is_nullable == "NO":
                col_parts.append("NOT NULL")

            column_definitions.append(" ".join(col_parts))

        # Build CREATE TABLE statement
        create_sql = f"CREATE TABLE {target_schema}.{target_table} (\n"
        create_sql += ",\n".join("  " + col_def for col_def in column_definitions)
        create_sql += "\n);"
        logger.info(f"CREATE TABLE statement: {create_sql}")
        return create_sql

    MAX_SAFE_NUMERIC_PRECISION = 18

    def _format_data_type(
        self,
        data_type: str,
        udt_name: Optional[str],
        numeric_precision: Optional[int],
        numeric_scale: Optional[int],
        character_maximum_length: Optional[int],
    ) -> str:
        data_type_lower = data_type.lower()

        # -------------------------
        # NUMERIC / DECIMAL
        # -------------------------
        if data_type_lower in ("numeric", "decimal"):
            # If precision is unknown, assume dangerous → downgrade
            if numeric_precision is None:
                return "DOUBLE PRECISION"

            # Precision too large → downgrade
            if numeric_precision > self.MAX_SAFE_NUMERIC_PRECISION:
                return "DOUBLE PRECISION"

            # Safe numeric
            if numeric_scale is not None:
                return f"NUMERIC({numeric_precision},{numeric_scale})"
            else:
                return f"NUMERIC({numeric_precision})"

        # -------------------------
        # CHARACTER TYPES
        # -------------------------
        if data_type_lower in ("character varying", "varchar"):
            if character_maximum_length is not None:
                return f"VARCHAR({character_maximum_length})"
            return "VARCHAR"

        if data_type_lower in ("character", "char"):
            if character_maximum_length is not None:
                return f"CHAR({character_maximum_length})"
            return "CHAR"

        # -------------------------
        # INTEGER TYPES
        # -------------------------
        if data_type_lower in ("smallint", "int2"):
            return "SMALLINT"
        if data_type_lower in ("integer", "int", "int4"):
            return "INTEGER"
        if data_type_lower in ("bigint", "int8"):
            return "BIGINT"

        # -------------------------
        # FLOATING POINT
        # -------------------------
        if data_type_lower in ("real", "float4"):
            return "REAL"
        if data_type_lower in ("double precision", "float8"):
            return "DOUBLE PRECISION"

        # -------------------------
        # BOOLEAN
        # -------------------------
        if data_type_lower in ("boolean", "bool"):
            return "BOOLEAN"

        # -------------------------
        # DATE / TIME
        # -------------------------
        if data_type_lower == "date":
            return "DATE"
        if data_type_lower in ("timestamp without time zone", "timestamp"):
            return "TIMESTAMP"
        if data_type_lower in ("timestamp with time zone", "timestamptz"):
            return "TIMESTAMP WITH TIME ZONE"
        if data_type_lower in ("time without time zone", "time"):
            return "TIME"
        if data_type_lower == "time with time zone":
            return "TIME WITH TIME ZONE"

        # -------------------------
        # TEXT / SUPER
        # -------------------------
        if data_type_lower == "text":
            return "TEXT"
        if data_type_lower == "super":
            return "TEXT"

        # -------------------------
        # FALLBACK
        # -------------------------
        return udt_name or data_type.upper()
