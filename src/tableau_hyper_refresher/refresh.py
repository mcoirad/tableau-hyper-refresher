"""Refresh job: Update hyper file with new data and publish to Tableau."""

import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict, cast

import tableauserverclient as TSC
from tableauhyperapi import (
    NOT_NULLABLE,
    NULLABLE,
    Connection,
    CreateMode,
    HyperProcess,
    SqlType,
    TableDefinition,
    TableName,
    Telemetry,
)
from tableauhyperapi.sql import escape_string_literal

from .polars_util import ColStats
from .redshift import RedshiftClient
from .refresh_config import RefreshConfig, load_refresh_config
from .secrets import get_json_secret
from .state import DynamoDBClient
from .storage import S3Client
from .strategies.base import StrategyContext
from .strategies.factory import RefreshStrategyFactory
from .tableau_client import TableauClient
from .tds import (
    create_tdsx_from_directory,
    extract_tds_from_tdsx,
    find_hyper_file,
    get_expected_hyper_path_from_tds,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger()

# COPY options per format
COPY_OPTS = {
    "csv": "WITH (format csv, header true)",
    "parquet": "WITH (format parquet)",
}


class DatasourceTableRef(TypedDict, total=False):
    table_key: str
    extract_table_name: str


class DatasourceDetails(TypedDict, total=False):
    latest_datasource_s3_key: str
    datasource_owner_email: str
    tables: List[DatasourceTableRef]


class TableDetails(TypedDict, total=False):
    table_id: str
    table_type: str
    s3_path: str
    column_names: List[str]
    extract_table_name: str
    table_key: str


@dataclass(frozen=True)
class TableRefreshInput:
    files: List[str]
    full_name: str
    extract_table_name: str


def hyper_sqltype(st: ColStats) -> SqlType:
    # Map your inferred types to Hyper SqlType
    if st.kind == "bool":
        return SqlType.bool()
    if st.kind == "int":
        return SqlType.big_int()
    if st.kind == "float":
        return SqlType.double()
    if st.kind == "decimal":
        p = max(st.dec_precision, 1)
        s = max(st.dec_scale, 0)
        return SqlType.numeric(p, s)
    if st.kind == "timestamp":
        return SqlType.timestamp()
    return SqlType.text()


def make_table_definition(
    table: TableName,
    cols: List[str],
    stats: List[ColStats],
) -> TableDefinition:
    """
    Build a Hyper TableDefinition using a TableName (schema+table),
    avoiding manual SQL and quoting issues.
    """
    if len(cols) != len(stats):
        raise ValueError(f"cols ({len(cols)}) and stats ({len(stats)}) must match")

    columns: List[TableDefinition.Column] = []
    for name, st in zip(cols, stats):
        nullability = NULLABLE if st.nullable else NOT_NULLABLE
        columns.append(
            TableDefinition.Column(
                name=name,
                type=hyper_sqltype(st),
                nullability=nullability,
            )
        )

    return TableDefinition(table_name=table, columns=columns)


def find_table_names(
    con: Connection, schema: str, replacing_table: str
) -> List[TableName]:
    """Find the matching table names in the hyper file schema for a given table."""
    table_names = con.catalog.get_table_names(schema)
    clean_replacing_name = get_clean_table_name_from_hyper_table_name(replacing_table)
    matched_names = [
        table_name
        for table_name in table_names
        if table_name.name.unescaped == clean_replacing_name
    ]
    if len(matched_names) == 0:
        raise RuntimeError(f"No tables matched for {replacing_table}")
    elif len(matched_names) > 1:
        logger.info(
            f"WARN: Multiple tables matched for {replacing_table}: {matched_names}"
        )
    return matched_names


def replace_table_ddl(
    con: Connection, tname: TableName, files: List[str], table_details: TableDetails
) -> bool:
    """
    Handle DDL operations (DROP/CREATE table) in a separate transaction.
    Returns True if table was recreated, False if columns matched.
    """

    redshift_client = RedshiftClient()
    # Check if columns match
    table_definition = con.catalog.get_table_definition(tname)
    logger.info(f"Table definition: {table_definition}")
    hyper_col_list = [col_def.name for col_def in table_definition.columns]
    redshift_col_list = table_details.get("column_names")
    if redshift_col_list is None:
        raise RuntimeError(f"No column_names found for {tname.name.unescaped}")
    do_columns_match = len(hyper_col_list) == len(redshift_col_list)
    # TODO: match on names as well

    if do_columns_match:
        logger.info(f"Columns match for {tname.name.unescaped}, skipping DDL")
        return False

    # Columns don't match - need to drop and recreate table
    logger.info(f"Columns don't match for {tname.name.unescaped}, recreating table")

    # DDL operations in their own transaction
    con.execute_command("BEGIN")
    try:
        con.execute_command(f"DROP TABLE {tname};")
        # cols, stats = infer_schema_from_csv_parts(files)
        # create_table_statement = make_create_table(tname, cols, stats)
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
        # logger.info(f"CREATE TABLE statement: {create_table_statement}")
        # table_def = make_table_definition(tname, cols, stats)
        # con.catalog.create_table(table_def)
        con.execute_command(create_table_statement)
        con.execute_command("COMMIT")
        logger.info(f"Successfully recreated table {tname.name.unescaped}")
    except Exception:
        con.execute_command("ROLLBACK")
        raise

    return True


def replace_table_dml(con: Connection, tname: TableName, files: List[str], fmt: str):
    """
    Handle DML operations (DELETE/COPY) in a separate transaction.
    """
    if not files:
        logger.info(
            f"WARNING: no files provided for {tname.name.unescaped}; leaving it empty."
        )
        return

    opts = COPY_OPTS[fmt]

    # DML operations in their own transaction
    con.execute_command("BEGIN")
    try:
        # Wipe rows (keeps schema/column order/metadata identical)
        con.execute_command(f"TRUNCATE TABLE {tname}")

        # COPY all files
        for f in files:
            con.execute_command(
                f"COPY {tname} FROM {escape_string_literal(os.path.abspath(f))} {opts};"
            )

        con.execute_command("COMMIT")
        logger.info(f"Successfully loaded data into {tname.name.unescaped}")
    except Exception:
        con.execute_command("ROLLBACK")
        raise

    # Log row count
    row_ct = list(con.execute_query(f"SELECT COUNT(*) FROM {tname}"))[0][0]
    logger.info(f"Loaded {row_ct:,} rows into {tname.name.unescaped}")


def replace_table(
    con: Connection,
    schema: str,
    replacing_table: str,
    files: List[str],
    table_details: TableDetails,
    fmt: str,
):
    """
    Replace data in a hyper table with new CSV/Parquet files.
    Separates DDL and DML into different transactions per table.

    Some datasources have multiple tables with the same name, so we need to update all of them.
    For example, mart_students_metrics_per_month_CD8B2ACBD0B14D9FB1FBAC8C494C0C8D
    and mart_students_metrics_per_month_CD8B2ACBD0B14D9FB1FBAC8C494C0C8D
    are both mart_students_metrics_per_month tables.
    We need to update both of them.
    """
    logger.info(
        f"Replacing table {replacing_table} in schema {schema} with details: {table_details}"
    )
    extract_table_name = table_details.get("extract_table_name")
    if not extract_table_name:
        raise RuntimeError(
            f"No extract_table_name provided for table {replacing_table}"
        )
    tnames = find_table_names(con, schema, extract_table_name)
    for tname in tnames:
        # First, handle DDL if needed (in its own transaction)
        replace_table_ddl(con, tname, files, table_details)

        # Then, handle DML (in its own transaction)
        replace_table_dml(con, tname, files, fmt)


def get_clean_table_name_from_hyper_table_name(hyper_table_name: str) -> str:
    """
    Get the clean table name from the hyper table name.
    """
    new_name = hyper_table_name.replace("[", "").replace("]", "").split(".")[-1]
    logger.info(f"Converted {hyper_table_name} -> {new_name}")
    return new_name


def match_correct_schema(
    con: Connection, local_csv_map: Dict[str, TableRefreshInput]
) -> str:
    """
    Match the correct schema based on the tables we expect to find in the schema.
    Tableau datasource table references appear to follow the format of: mart_tutors_metrics_per_month_CD8B2ACBD0B14D9FB1FBAC8C494C0C8D
    so tables are matched by the first part of the name.
    """
    schema_names = con.catalog.get_schema_names()
    tables_found = []
    for schema_name in schema_names:
        tables_in_schema = con.catalog.get_table_names(schema_name)
        if len(tables_in_schema) == 0:
            logger.info(f"Schema {schema_name} has no tables, skipping")
            continue
        else:
            local_csv_map_keys = list(local_csv_map.keys())
            for expected_table in local_csv_map_keys:
                expected_hyper_table_name = local_csv_map[
                    expected_table
                ].extract_table_name
                matched_tables = [
                    table_tbl
                    for table_tbl in tables_in_schema
                    if get_clean_table_name_from_hyper_table_name(
                        expected_hyper_table_name
                    )
                    == table_tbl.name.unescaped
                ]
                logger.info(f"{[tbl.name.unescaped for tbl in tables_in_schema]}")
                if len(matched_tables) == 1:
                    return schema_name
                elif len(matched_tables) > 1:
                    logger.info(
                        f"WARN: Multiple tables matched for {expected_table}: {matched_tables}"
                    )
                    # This should be fine, we update both referenced tables
                    return schema_name
                else:
                    logger.info(f"No tables matched for {expected_hyper_table_name}")
        tables_found.extend(tables_in_schema)
    raise RuntimeError(
        f"No schema with correct of tables found. Expected tables:{local_csv_map.keys()}, found: {tables_found}"
    )


def get_tableau_redshift_credentials(
    secret_name: str | None = None,
) -> Dict[str, str]:
    """Fetch Redshift credentials from AWS Secrets Manager."""
    region = os.environ.get("AWS_REGION", "us-west-2")
    secret_name = secret_name or os.environ.get("TABLEAU_REDSHIFT_SECRET_NAME")
    if not secret_name:
        raise ValueError("Set TABLEAU_REDSHIFT_SECRET_NAME")
    secret = get_json_secret(secret_name, region)
    username = secret.get("username")
    password = secret.get("password")
    if not username or not password:
        raise RuntimeError(
            f"Missing required username/password keys in AWS secret '{secret_name}'."
        )
    return {"username": username, "password": password}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for refresh job."""
    parser = argparse.ArgumentParser(
        description="Refresh Tableau extract with new data"
    )
    parser.add_argument("--datasource-id", required=True, help="Tableau datasource ID")
    parser.add_argument(
        "--s3-bucket", help="S3 bucket for CSV data (defaults to AWS_BUCKET env var)"
    )
    parser.add_argument(
        "--format", default="csv", choices=["csv", "parquet"], help="Data format"
    )
    parser.add_argument(
        "--refresh-config",
        help="Optional path to YAML refresh strategy config",
    )
    return parser.parse_args()


class RefreshJob:
    """Orchestrates refresh steps while preserving current CLI behavior."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.tableau_client = TableauClient()
        self.s3_client = S3Client()
        self.dynamo_client = DynamoDBClient()

        self.datasource_owner_email: Optional[str] = None
        self.datasource_details: Optional[DatasourceDetails] = None
        self.tables_info: Dict[str, TableDetails] = {}
        self.local_csv_map: Dict[str, TableRefreshInput] = {}

        self.s3_bucket = args.s3_bucket or os.environ.get("AWS_BUCKET")
        if not self.s3_bucket:
            raise ValueError("Set --s3-bucket or AWS_BUCKET")
        self.format_type = args.format

        self.tdsx_file_local = "/tmp/datasource_structure.tdsx"
        self.extracted_dir = "/tmp/tds_extracted_refresh"
        self.tmpdir = "/tmp/csvs"
        self.tdsx_output = "/tmp/refreshed_datasource.tdsx"

        self.expected_hyper_filename: Optional[str] = None
        self.hyper_file_local: Optional[str] = None
        self.new_hyper_path: Optional[str] = None
        self.new_hyper_file_s3_key: Optional[str] = None
        self.refresh_config: RefreshConfig = self._load_refresh_config(
            getattr(args, "refresh_config", None)
        )
        self.resolved_table_configs: Dict[str, Any] = {}

    @staticmethod
    def _load_refresh_config(config_path: Optional[str]) -> RefreshConfig:
        if not config_path:
            logger.info(
                "No refresh config provided; defaulting to full_replace for all tables"
            )
            return RefreshConfig()

        path = Path(config_path)
        if not path.exists():
            raise RuntimeError(f"Refresh config path does not exist: {config_path}")

        logger.info(f"Loading refresh config from {config_path}")
        config = load_refresh_config(config_path)
        logger.info(
            f"Loaded refresh config version={config.version} with {len(config.tables)} table override(s)"
        )
        return config

    def run(self) -> None:
        os.chdir("/tmp")
        self._load_datasource_details()
        self._download_and_extract_tdsx()
        self._download_table_files()
        self._refresh_hyper()
        self._create_output_paths_and_package()
        self._upload_outputs()
        self._publish_datasource()
        self._update_watermark()

    def _load_datasource_details(self) -> None:
        datasource_details_raw = self.dynamo_client.get_tableau_datasource(
            self.args.datasource_id
        )
        if datasource_details_raw is None:
            raise RuntimeError(f"No table details found for {self.args.datasource_id}")
        self.datasource_details = cast(DatasourceDetails, datasource_details_raw)
        self.datasource_owner_email = self.datasource_details.get(
            "datasource_owner_email"
        )
        if self.datasource_owner_email:
            logger.info(
                f"Authenticating Tableau client as datasource owner: {self.datasource_owner_email}"
            )
        else:
            logger.info(
                "No datasource_owner_email in watermark; using default Tableau user from secret"
            )
        self.tableau_client = TableauClient(user_email=self.datasource_owner_email)

        tables = self.datasource_details.get("tables")
        if not tables:
            raise RuntimeError(f"No tables found for {self.args.datasource_id}")

        for table in tables:
            table_key = table.get("table_key")
            if not table_key:
                raise RuntimeError(
                    f"No table key found for datasource {self.args.datasource_id}"
                )
            table_details_raw = self.dynamo_client.get_tableau_table(table_key)
            if table_details_raw is None:
                raise RuntimeError(
                    f"No table details found for {self.args.datasource_id}.{table_key}"
                )
            table_details = cast(TableDetails, table_details_raw)
            table_details.update(table)
            self.tables_info[table_key] = table_details

        logger.info(f"Refreshing extract for {len(tables)} tables: {tables}")
        logger.info(f"Tables info: {self.tables_info}")

    def _download_and_extract_tdsx(self) -> None:
        if self.datasource_details is None:
            raise RuntimeError("Datasource details are not loaded")
        latest_datasource_s3_key = self.datasource_details.get(
            "latest_datasource_s3_key"
        )
        if not latest_datasource_s3_key:
            raise RuntimeError(
                f"No latest_datasource_s3_key for {self.args.datasource_id}"
            )

        retained_bucket = self.s3_client.retained_bucket
        logger.info(
            f"Downloading tdsx file from {retained_bucket}/{latest_datasource_s3_key} "
            f"to {self.tdsx_file_local}"
        )
        self.s3_client.download_file(
            bucket=retained_bucket,
            key=latest_datasource_s3_key,
            local_path=self.tdsx_file_local,
        )
        logger.info(f"Downloaded tdsx file to {self.tdsx_file_local}")

        if os.path.exists(self.extracted_dir):
            shutil.rmtree(self.extracted_dir)

        tds_file_path = extract_tds_from_tdsx(self.tdsx_file_local, self.extracted_dir)

        expected_hyper_path = get_expected_hyper_path_from_tds(
            tds_file_path, self.extracted_dir
        )
        self.expected_hyper_filename = Path(expected_hyper_path).name
        self.hyper_file_local = find_hyper_file(self.extracted_dir)
        if not self.hyper_file_local:
            raise RuntimeError("No .hyper file found in datasource")
        logger.info(f"Expected hyper file: {self.expected_hyper_filename}")

    def _download_table_files(self) -> None:
        for table_fq, table_info in self.tables_info.items():
            logger.info(f"Processing table: {table_fq}:{table_info}")
            if table_info.get("table_type") == "text":
                continue
            if table_info.get("table_type") != "table":
                continue

            schema, table = table_fq.split(".", 1)
            s3_path = table_info.get("s3_path")
            if not s3_path:
                raise RuntimeError(f"No latest unload s3_path found for {table_fq}")
            logger.info(f"Using latest unload for {schema}.{table}: {s3_path}")

            local_dir = os.path.join(self.tmpdir, f"{schema}_{table}".replace('"', ""))
            os.makedirs(local_dir, exist_ok=True)
            logger.info(f"Downloading CSV files from {s3_path} to {local_dir}")

            path_parts = s3_path[5:].split("/", 1)
            bucket_name = path_parts[0]
            s3_key = path_parts[1] if len(path_parts) > 1 else ""
            self.s3_client.download_folder(bucket_name, s3_key, local_dir)

            csvs = []
            for root, _, files in os.walk(local_dir):
                for f in files:
                    csvs.append(os.path.join(root, f))

            if not csvs:
                raise RuntimeError(
                    f"No CSVs found for {schema}.{table} under {local_dir}"
                )
            extract_table_name = table_info.get("extract_table_name")
            if not extract_table_name:
                raise RuntimeError(f"No extract_table_name found for {schema}.{table}")

            # Map table name (without schema) to files
            # We now have the exact table name in the hyper file
            self.local_csv_map[table] = TableRefreshInput(
                files=sorted(csvs),
                full_name=table_fq,
                extract_table_name=extract_table_name,
            )

        logger.info(f"Local CSV map: {self.local_csv_map}")

    def _refresh_hyper(self) -> None:
        if self.hyper_file_local is None:
            raise RuntimeError("Local hyper file path is not set")
        if self.format_type not in COPY_OPTS:
            raise ValueError("FORMAT must be 'csv' or 'parquet'")

        process_parameters = {
            "default_database_version": "3",
        }
        logger.info(f"Process parameters: {process_parameters}")

        with HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters=process_parameters
        ) as hp:
            with Connection(
                hp.endpoint, database=self.hyper_file_local, create_mode=CreateMode.NONE
            ) as con:
                schema_names = con.catalog.get_schema_names()
                logger.info(f"Available schemas: {schema_names}")
                schema_name = match_correct_schema(con, self.local_csv_map)
                logger.info(f"Using schema: {schema_name}")

                for replacing_table, csv_info in self.local_csv_map.items():
                    logger.info(f"\n=== Processing table: {replacing_table} ===")
                    table_config = self.refresh_config.for_table(csv_info.full_name)
                    self.resolved_table_configs[csv_info.full_name] = table_config
                    strategy = RefreshStrategyFactory.for_mode(table_config.mode)
                    logger.info(
                        f"Resolved strategy {strategy.__class__.__name__} for {csv_info.full_name} (mode={table_config.mode})"
                    )
                    strategy.apply(
                        con,
                        StrategyContext(
                            schema_name=schema_name,
                            table_name=replacing_table,
                            source_files=csv_info.files,
                            file_format=self.format_type,
                            table_config=table_config,
                            table_details=dict(self.tables_info[csv_info.full_name]),
                        ),
                    )

        logger.info(f"✔ Updated {self.hyper_file_local} successfully.")

    def _create_output_paths_and_package(self) -> None:
        if self.expected_hyper_filename is None:
            raise RuntimeError("Expected hyper filename is not set")
        extracts_dir = None
        for root, dirs, files in os.walk(self.extracted_dir):
            if "Extracts" in dirs:
                extracts_dir = os.path.join(root, "Extracts")
                break
            for f in files:
                if f.endswith(".hyper"):
                    extracts_dir = root
                    break
            if extracts_dir:
                break

        if not extracts_dir:
            extracts_dir = os.path.join(self.extracted_dir, "Data", "Extracts")
            os.makedirs(extracts_dir, exist_ok=True)

        self.new_hyper_path = os.path.join(extracts_dir, self.expected_hyper_filename)
        create_tdsx_from_directory(self.extracted_dir, self.tdsx_output)

    def _upload_outputs(self) -> None:
        if self.new_hyper_path is None:
            raise RuntimeError("New hyper path is not set")
        self.new_hyper_file_s3_key = self.s3_client.upload_hyper_file(
            hyper_file_path=self.new_hyper_path, datasource_id=self.args.datasource_id
        )

        self.s3_client.upload_file(
            local_path=self.tdsx_output,
            bucket=self.s3_client.retained_bucket,
            key=f"{self.args.datasource_id}/refreshed_datasource.tdsx",
        )

    def _publish_datasource(self) -> None:
        ds = self.tableau_client.get_datasource(self.args.datasource_id)
        project_id = ds.project.id
        new_ds = TSC.DatasourceItem(project_id, name=ds.name)

        creds = get_tableau_redshift_credentials()
        logger.info(f"Using Redshift credentials for user {creds['username']}")

        logger.info(
            f"Publishing refreshed datasource to Tableau from {self.tdsx_output}"
        )
        self.tableau_client.publish_datasource_with_embedded_credentials(
            new_ds,
            self.tdsx_output,
            creds["username"],
            creds["password"],
            mode=TSC.Server.PublishMode.Overwrite,
        )
        logger.info("✔ Published refreshed datasource to Tableau")
        self._advance_table_watermarks()

    def _advance_table_watermarks(self) -> None:
        """Advance table and datasource-table last_applied_sort_value after successful publish."""
        updated_count = 0
        for table_fq, table_details in self.tables_info.items():
            table_config = self.resolved_table_configs.get(table_fq)
            if table_config is None:
                table_config = self.refresh_config.for_table(table_fq)

            is_incremental = (
                table_config.mode in ("upsert", "merge")
                and table_config.incremental.enabled
            )
            if not is_incremental:
                continue

            table_id = table_details.get("table_id") or table_fq
            candidate = table_details.get("max_sort_column_value")
            if not candidate:
                logger.info(
                    f"Skipping watermark advance for {table_id}: no max_sort_column_value found"
                )
                continue

            self.dynamo_client.update_tableau_table(
                table_id,
                {"last_applied_sort_value": str(candidate)},
            )
            self.dynamo_client.update_tableau_datasource_tables(
                self.args.datasource_id,
                table_fq,
                {"last_applied_sort_value": str(candidate)},
            )
            updated_count += 1
            logger.info(
                f"Updated last_applied_sort_value for table={table_id} and datasource_table={self.args.datasource_id}:{table_fq} to {candidate}"
            )

        logger.info(
            f"Watermark advancement complete. Updated {updated_count} table(s)."
        )

    def _update_watermark(self) -> None:
        if self.new_hyper_file_s3_key is None:
            raise RuntimeError("New hyper file S3 key is not set")
        watermark: Dict[str, Any] = {
            "hyper_file_s3_key": self.new_hyper_file_s3_key,
            "this_record_updated_at": datetime.now(UTC).isoformat(),
        }
        self.dynamo_client.update_tableau_datasources(
            self.args.datasource_id, watermark
        )
        logger.info(
            f"✔ Updated hyper file at s3://{self.s3_client.expiring_bucket}/{self.new_hyper_file_s3_key}"
        )


def main():
    """Main entry point for refresh job."""
    args = parse_args()

    RefreshJob(args).run()


if __name__ == "__main__":
    main()
