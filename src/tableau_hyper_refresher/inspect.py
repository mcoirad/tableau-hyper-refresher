"""Inspect job: Get list of tables used by a Tableau datasource with watermark caching."""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
from datetime import UTC, datetime
from typing import Any, Dict, List

from .redshift import RedshiftClient
from .settings import ColumnMetadataField
from .state import DynamoDBClient
from .storage import S3Client
from .tableau_client import TableauClient
from .tds import extract_tables_from_tds, extract_tds_from_tdsx

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger()


def _schema_signature(columns: List[List[Any]]) -> str:
    """Build a deterministic hash for Redshift table column metadata."""
    normalized_columns = [
        {
            "column_name": str(row[ColumnMetadataField.COLUMN_NAME]).lower()
            if row[ColumnMetadataField.COLUMN_NAME] is not None
            else None,
            "data_type": str(row[ColumnMetadataField.DATA_TYPE]).lower()
            if row[ColumnMetadataField.DATA_TYPE] is not None
            else None,
            "udt_name": str(row[ColumnMetadataField.UDT_NAME]).lower()
            if row[ColumnMetadataField.UDT_NAME] is not None
            else None,
            "is_nullable": str(row[ColumnMetadataField.IS_NULLABLE]).upper()
            if row[ColumnMetadataField.IS_NULLABLE] is not None
            else None,
            "numeric_precision": row[ColumnMetadataField.NUMERIC_PRECISION],
            "numeric_scale": row[ColumnMetadataField.NUMERIC_SCALE],
        }
        for row in columns
    ]
    payload = json.dumps(
        normalized_columns, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_force_full_load(
    dynamo_client: DynamoDBClient,
    redshift_client: RedshiftClient,
    table: Dict[str, Any],
    override_force_full_load: bool,
) -> Dict[str, Any]:
    """Resolve force_full_load based on Redshift schema signature changes."""
    table_type = table.get("type")
    if table_type != "table":
        return {
            "force_full_load": override_force_full_load,
            "redshift_schema_signature": None,
        }

    table_name = table.get("table")
    table_key = table.get("table_key")
    if not table_name or "." not in table_name:
        raise ValueError(f"Invalid table name for table_key={table_key}: {table_name}")
    if not table_key:
        raise ValueError(f"Missing table_key for table={table_name}")

    table_schema, table_relation = table_name.split(".", 1)
    columns = redshift_client.get_table_columns(table_schema, table_relation)
    current_signature = _schema_signature(columns)

    if override_force_full_load:
        logger.info(
            f"Global force_full_load override enabled for {table_name} ({table_key})"
        )
        return {
            "force_full_load": True,
            "redshift_schema_signature": current_signature,
        }

    table_state = dynamo_client.get_tableau_table(table_key) or {}
    previous_signature = table_state.get("redshift_schema_signature")
    force_full_load = bool(
        previous_signature and previous_signature != current_signature
    )

    if force_full_load:
        logger.info(
            f"Schema change detected for {table_name} ({table_key}); forcing full load"
        )
    else:
        logger.info(
            f"No schema change for {table_name} ({table_key}); full load not forced"
        )

    return {
        "force_full_load": force_full_load,
        "redshift_schema_signature": current_signature,
    }


def main():
    """Main entry point for inspect job."""
    parser = argparse.ArgumentParser(
        description="Inspect Tableau datasource for tables"
    )
    parser.add_argument("--datasource-id", required=True, help="Tableau datasource ID")
    parser.add_argument(
        "--force-full-load",
        choices=["true", "false"],
        default="false",
        help="Force full load for all tables in this datasource. Allowed: true|false. Default: false",
    )
    args = parser.parse_args()
    force_full_load = args.force_full_load == "true"

    # Switch to /tmp for Lambda/Batch
    os.chdir("/tmp")

    sf_execution_id = os.environ.get("STEP_FUNCTION_EXECUTION_ID")
    if sf_execution_id is None:
        raise RuntimeError("STEP_FUNCTION_EXECUTION_ID is not set")

    tableau_client = TableauClient()
    s3_client = S3Client()
    redshift_client = RedshiftClient()
    dynamo_client = DynamoDBClient()

    # Get datasource metadata
    ds = tableau_client.get_datasource(args.datasource_id)
    ds_name = ds.name
    logger.info(f"Datasource name: {ds_name}")
    logger.info(f"Datasource ID: {ds.id}")
    datasource_updated_at = ds.updated_at
    datasource_owner_id = getattr(ds, "owner_id", None)
    if not datasource_owner_id:
        raise RuntimeError(f"Datasource {args.datasource_id} has no owner_id")
    datasource_owner_email = tableau_client.get_user_email(datasource_owner_id)
    logger.info(f"Datasource owner email: {datasource_owner_email}")

    # Get watermark
    # watermark = s3_client.get_watermark(args.datasource_id)
    logger.info("Getting watermark from dynamo")
    watermark = dynamo_client.get_tableau_datasource(args.datasource_id)

    # Check if we need to download
    needs_download = False
    if watermark is None:
        logger.info("No watermark found, downloading datasource...")
        needs_download = True
    else:
        last_refresh_ts = watermark.get("this_record_updated_at")
        if last_refresh_ts:
            last_refresh_dt = datetime.fromisoformat(
                last_refresh_ts.replace("Z", "+00:00")
            )
            if datasource_updated_at > last_refresh_dt:
                logger.info(
                    f"Datasource modified ({datasource_updated_at}) after last refresh ({last_refresh_dt}), downloading..."
                )
                needs_download = True
            else:
                logger.info(
                    "Datasource not modified since last refresh, using cached tables"
                )
        else:
            logger.info("Watermark missing last_refresh_ts, downloading datasource...")
            needs_download = True

    if force_full_load and watermark and not watermark.get("tables"):
        logger.info(
            "force-full-load override set and cached watermark has no tables; downloading datasource metadata"
        )
        needs_download = True

    tables: List[Dict[str, Any]] = []

    if needs_download:
        # Download datasource
        logger.info("Downloading datasource...")
        ds_file_path = "/tmp/tableau_datasource"
        out_path = tableau_client.download_datasource(
            datasource_id=args.datasource_id,
            filepath=ds_file_path,
            include_extract=True,
        )
        logger.info(f"Downloaded to: {out_path}")

        ds_s3_key = f"{args.datasource_id}/tableau_datasource.tdsx"
        s3_client.upload_file(
            local_path=out_path, bucket=s3_client.retained_bucket, key=ds_s3_key
        )

        logger.info(f"Uploaded tds to {ds_s3_key}")

        # Extract
        extracted_dir = "/tmp/tds_extracted"
        if os.path.exists(extracted_dir):
            if os.path.isdir(extracted_dir):
                shutil.rmtree(extracted_dir)
            else:
                os.remove(extracted_dir)

        tds_file_path = extract_tds_from_tdsx(out_path, extracted_dir)

        # Extract tables from TDS
        tables = extract_tables_from_tds(tds_file_path)
        logger.info(f"Found {len(tables)} tables: {tables}")

        # Update watermark
        watermark = {
            "this_record_updated_at": datetime.now(UTC).isoformat(),
            "datasource_updated_at": datasource_updated_at.isoformat() + "Z",
            "tables": sorted(list(tables), key=lambda x: str(x.get("table_key", ""))),
            "last_seen_by_sf_execution_id": sf_execution_id,
            "latest_datasource_s3_key": ds_s3_key,
            "datasource_name": ds_name,
        }
        if datasource_owner_email:
            watermark["datasource_owner_email"] = datasource_owner_email
        dynamo_client.update_tableau_datasources(args.datasource_id, watermark)
        # s3_client.put_watermark(args.datasource_id, watermark)
        logger.info("\n=== INSPECT RESULTS ===")
        logger.info(
            f"Tables: {sorted(tables, key=lambda x: str(x.get('table_key', '')))}"
        )
        logger.info(f"Watermark updated: {needs_download}")
    else:
        logger.info("Using cached tables from dynamodb")
        tables = (watermark or {}).get("tables", [])
        watermark = {
            "last_seen_by_sf_execution_id": sf_execution_id,
        }
        if datasource_owner_email:
            watermark["datasource_owner_email"] = datasource_owner_email
        watermark["datasource_owner_id"] = datasource_owner_id
        dynamo_client.update_tableau_datasources(args.datasource_id, watermark)

    for table in tables:
        table_key = table.get("table_key")
        if not table_key:
            raise ValueError(f"Missing table_key in table metadata: {table}")

        force_data = _resolve_force_full_load(
            dynamo_client,
            redshift_client,
            table,
            force_full_load,
        )
        table_type = table.get("type")
        if table_type == "table":
            table_reference = table.get("table")
            if not isinstance(table_reference, str):
                raise ValueError(f"Missing table reference in metadata: {table}")
            table_schema, table_name = table_reference.split(".", 1)
            query_text = None
        elif table_type == "text":
            table_schema = None
            text_table_name = table.get("table")
            if not isinstance(text_table_name, str):
                raise ValueError(f"Missing logical table name in metadata: {table}")
            table_name = text_table_name
            query_text = table.get("query_text")
        else:
            raise ValueError(f"Unknown table type: {table_type}")

        now = datetime.now(UTC).isoformat()
        dynamo_client.update_tableau_datasource_tables(
            args.datasource_id,
            table_key,
            {
                "table_schema": table_schema,
                "table_name": table_name,
                "this_record_updated_at": now,
                "query_text": query_text,
                "force_full_load": force_data["force_full_load"],
                "redshift_schema_signature": force_data["redshift_schema_signature"],
            },
        )
        dynamo_client.update_tableau_table(
            table_key,
            {
                "this_record_updated_at": now,
                "force_full_load": force_data["force_full_load"],
                "redshift_schema_signature": force_data["redshift_schema_signature"],
            },
        )


if __name__ == "__main__":
    main()
