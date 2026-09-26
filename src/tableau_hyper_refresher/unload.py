"""Unload job: Export a Redshift table to S3."""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .redshift import RedshiftClient
from .refresh_config import RefreshConfig, TableRefreshConfig, load_refresh_config
from .settings import ColumnMetadataField, get_env
from .state import DynamoDBClient
from .tds import get_datetime_str

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger()

DATASOURCE_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def get_table_column_names(schema: str, table: str) -> List[str]:
    """
    Retrieve a list of column names for the specified Redshift table.

    Args:
        schema: Redshift schema name
        table: Redshift table name

    Returns:
        List of column names in order
    """
    redshift_client = RedshiftClient()
    columns_metadata = redshift_client.get_table_columns(schema, table)

    # Extract column names from the metadata (first element in each row)
    column_names = [row[ColumnMetadataField.COLUMN_NAME] for row in columns_metadata]
    return column_names


def resolve_refresh_config(
    config_path: Optional[str], table_id: str
) -> TableRefreshConfig:
    """Resolve table config; defaults to full_replace when no config path is provided."""
    if not config_path:
        return RefreshConfig().for_table(table_id)
    config = load_refresh_config(config_path)
    return config.for_table(table_id)


def to_utc_iso_with_lookback(timestamp_str: str, lookback_minutes: int) -> str:
    """Return ISO UTC timestamp after subtracting lookback minutes."""
    parsed = parse_utc_datetime(timestamp_str)
    shifted = parsed - timedelta(minutes=lookback_minutes)
    return shifted.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_datetime(timestamp_str: str) -> datetime:
    """Parse timestamp-like string and normalize to timezone-aware UTC datetime."""
    normalized = timestamp_str.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_table_select_sql(
    schema: str,
    table: str,
    table_config: TableRefreshConfig,
    last_applied_sort_value: Optional[str],
    force_full_load: bool,
) -> str:
    """
    Build SELECT SQL for table unload.
    - full_replace: full table unload
    - upsert/merge + incremental enabled: unload from watermark-lookback onward
    """
    base_sql = f"SELECT * FROM {schema}.{table}"

    if force_full_load:
        logger.info(f"force_full_load=True for {schema}.{table}; using full unload")
        return base_sql

    incremental_enabled = table_config.incremental.enabled
    is_incremental_mode = table_config.mode in ("upsert", "merge")
    if not (incremental_enabled and is_incremental_mode):
        logger.info(
            f"Using full unload for {schema}.{table} (mode={table_config.mode}, incremental_enabled={incremental_enabled})"
        )
        return base_sql

    if not table_config.sort_column:
        raise ValueError(
            f"{schema}.{table}: sort_column is required for mode={table_config.mode}"
        )

    if not last_applied_sort_value:
        logger.info(
            f"No last_applied_sort_value found for {schema}.{table}; falling back to full unload"
        )
        return base_sql

    lower_bound = to_utc_iso_with_lookback(
        last_applied_sort_value, table_config.incremental.lookback_minutes
    )
    lower_bound_sql = lower_bound.replace("'", "''")
    sql = f"{base_sql} WHERE {table_config.sort_column} >= '{lower_bound_sql}'"
    logger.info(
        f"Using incremental unload for {schema}.{table}: {table_config.sort_column} >= {lower_bound}"
    )
    return sql


def parse_datasource_ids(raw: Optional[str]) -> List[str]:
    """Parse datasource IDs from comma-separated string."""
    if not raw:
        return []
    datasource_ids = []
    for token in raw.split(","):
        ds_id = token.strip()
        if not ds_id:
            continue
        if not DATASOURCE_ID_PATTERN.match(ds_id):
            raise ValueError(f"Invalid datasource ID format: {ds_id}")
        datasource_ids.append(ds_id)
    return sorted(set(datasource_ids))


def resolve_effective_last_applied_sort_value(
    dynamo_client: DynamoDBClient,
    table_key: str,
    table_last_applied_sort_value: Optional[str],
    datasource_ids: List[str],
) -> Optional[str]:
    """
    Resolve the incremental lower-bound source for a shared unload.

    Rules:
    - If datasource list is empty, keep legacy table watermark behavior.
    - If any datasource-table watermark is missing, return None (full catch-up unload).
    - Otherwise, use the earliest watermark across datasource-table rows and the table watermark.
    """
    if not datasource_ids:
        logger.info(
            f"{table_key}: no datasource IDs provided; using legacy table watermark={table_last_applied_sort_value}"
        )
        return table_last_applied_sort_value

    datasource_values: List[datetime] = []
    for datasource_id in datasource_ids:
        ds_table_state = dynamo_client.get_tableau_datasource_table(
            datasource_id, table_key
        )
        ds_value = (ds_table_state or {}).get("last_applied_sort_value")
        if not ds_value:
            logger.info(
                f"{table_key}: datasource {datasource_id} has no last_applied_sort_value; forcing full catch-up unload"
            )
            return None
        datasource_values.append(parse_utc_datetime(str(ds_value)))

    candidates = list(datasource_values)
    if table_last_applied_sort_value:
        candidates.append(parse_utc_datetime(str(table_last_applied_sort_value)))

    if not candidates:
        return None

    effective = min(candidates).isoformat().replace("+00:00", "Z")
    logger.info(
        f"{table_key}: resolved effective watermark {effective} from datasource_count={len(datasource_values)} table_watermark={table_last_applied_sort_value}"
    )
    return effective


def get_max_sort_column_value(
    redshift_client: RedshiftClient,
    select_sql: str,
    table_config: TableRefreshConfig,
    table_key: str,
) -> Optional[str]:
    """Compute max sort-column value for incremental tables using the exact unload dataset."""
    is_incremental_mode = table_config.mode in ("upsert", "merge")
    if not (is_incremental_mode and table_config.incremental.enabled):
        return None
    if not table_config.sort_column:
        raise ValueError(
            f"{table_key}: sort_column is required for mode={table_config.mode}"
        )

    max_sql = (
        f"SELECT MAX({table_config.sort_column}) AS max_sort_value "
        f"FROM ({select_sql}) AS source_rows"
    )
    rows = redshift_client.execute(max_sql, with_results=True) or []
    if not rows or len(rows[0]) == 0 or rows[0][0] is None:
        logger.info(
            f"No max sort value found for {table_key} in current unload dataset"
        )
        return None

    max_value = str(rows[0][0])
    logger.info(
        f"Computed max sort value for {table_key}: {table_config.sort_column}={max_value}"
    )
    return max_value


def main():
    """Main entry point for unload job."""
    parser = argparse.ArgumentParser(description="Unload Redshift table to S3")
    parser.add_argument(
        "--table-text",
        "--table_text",
        dest="table_text",
        required=True,
        help="Either a redshift table name or custom SQL Text",
    )
    parser.add_argument(
        "--table-key",
        "--table_key",
        dest="table_key",
        required=True,
        help="Key of the table in DynamoDB",
    )
    parser.add_argument(
        "--table-type",
        "--table_type",
        dest="table_type",
        required=True,
        help="Type of the table, can be 'table' or 'text'",
    )
    parser.add_argument(
        "--s3-bucket", help="S3 bucket for unload (defaults to AWS_BUCKET env var)"
    )
    parser.add_argument(
        "--s3-prefix", help="S3 prefix for unload (defaults to AWS_PREFIX env var)"
    )
    parser.add_argument(
        "--refresh-config",
        help="Optional path to YAML refresh strategy config",
    )
    parser.add_argument(
        "--datasource-ids",
        help="Comma-separated datasource IDs associated with this table",
    )
    args = parser.parse_args()
    logger.info(args)

    if args.table_type not in ["table", "text"]:
        raise ValueError(f"Invalid table type: {args.table_type}")

    redshift_client = RedshiftClient()
    dynamo_client = DynamoDBClient()

    # Get S3 config
    s3_bucket = args.s3_bucket or get_env("AWS_BUCKET")
    s3_prefix = args.s3_prefix or get_env("AWS_PREFIX", "tables")
    iam_role_arn = get_env("REDSHIFT_UNLOAD_ROLE_ARN")

    # Unload table to S3

    logger.info(f"Unloading {args.table_text} to S3...")

    if args.table_type == "table":
        schema, table = args.table_text.split(".")
        column_names = get_table_column_names(schema, table)
        logger.info(f"Column names: {column_names}")
    else:
        schema, table = args.table_key.split("_")
        column_names = None  # TODO: parse from the custom SQL

    table_refresh_config = resolve_refresh_config(args.refresh_config, args.table_key)
    table_state = dynamo_client.get_tableau_table(args.table_key) or {}
    table_last_applied_sort_value = table_state.get("last_applied_sort_value")
    datasource_ids = parse_datasource_ids(args.datasource_ids)
    effective_last_applied_sort_value = resolve_effective_last_applied_sort_value(
        dynamo_client=dynamo_client,
        table_key=args.table_key,
        table_last_applied_sort_value=table_last_applied_sort_value,
        datasource_ids=datasource_ids,
    )
    force_full_load = bool(table_state.get("force_full_load", False))
    logger.info(
        f"Resolved table config for {args.table_key}: mode={table_refresh_config.mode}, "
        f"incremental_enabled={table_refresh_config.incremental.enabled}, "
        f"table_last_applied_sort_value={table_last_applied_sort_value}, "
        f"effective_last_applied_sort_value={effective_last_applied_sort_value}, "
        f"datasource_count={len(datasource_ids)}, "
        f"force_full_load={force_full_load}"
    )

    if args.table_type == "table":
        select_sql = build_table_select_sql(
            schema=schema,
            table=table,
            table_config=table_refresh_config,
            last_applied_sort_value=effective_last_applied_sort_value,
            force_full_load=force_full_load,
        )
    elif args.table_type == "text":
        if table_refresh_config.mode in ("upsert", "merge"):
            logger.info(
                "Custom SQL table configured for incremental mode; using provided SQL text as-is for now"
            )
        select_sql = args.table_text
    else:
        raise ValueError(f"Invalid table type: {args.table_type}")

    datetime_str = get_datetime_str()
    s3_path = f"s3://{s3_bucket}/{s3_prefix}/{schema}_{table}/{datetime_str}/"

    # Set statement timeout
    redshift_client.execute("SET statement_timeout = 900000;")  # 15 minutes

    unload_sql = f"""
        UNLOAD ('{select_sql.replace("'", "''").replace(">>", ">").replace("<<", "<")}')
        TO '{s3_path}'
        IAM_ROLE '{iam_role_arn}'
        CSV
        HEADER
        ALLOWOVERWRITE
        PARALLEL ON
        ;
    """

    logger.info(unload_sql)
    redshift_client.execute(unload_sql, with_results=False)
    max_sort_column_value = None
    if args.table_type == "table":
        max_sort_column_value = get_max_sort_column_value(
            redshift_client=redshift_client,
            select_sql=select_sql,
            table_config=table_refresh_config,
            table_key=args.table_key,
        )
    time_of_unload = datetime.now(timezone.utc).isoformat()
    logger.info(f"Unloaded {args.table_text} to {s3_path}")

    # Output S3 path for downstream jobs
    logger.info("\n=== UNLOAD RESULTS ===")
    logger.info(f"S3 path: {s3_path}")

    update_payload = {
        "s3_path": s3_path,
        "time_of_unload": time_of_unload,
        "column_names": column_names,
        "table_type": args.table_type,
    }
    if max_sort_column_value is not None:
        update_payload["max_sort_column_value"] = max_sort_column_value

    dynamo_client.update_tableau_table(args.table_key, update_payload)


if __name__ == "__main__":
    main()
