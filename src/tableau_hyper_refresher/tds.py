"""Tableau datasource archive and metadata helpers."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from lxml import etree as ET

logger = logging.getLogger(__name__)


def get_custom_sql_query_table_key(table_name: str, query_text: str) -> str:
    """Get the table key for a custom SQL query.
    Args:
        query_text: The text of the custom SQL query
    Returns:
        The table key for the custom SQL query
    """
    hashed_query_text = hashlib.sha256(query_text.encode()).hexdigest()
    return f"{table_name}_{hashed_query_text}"


def extract_tables_from_tds(tds_file_path: str) -> List[Dict]:
    """Extract table names from a .tds XML file.

    Args:
        tds_file_path: The path to the .tds file
    Returns:
        A list of dictionaries, where each dictionary represents a table

    table = {
        "type": type of table relation, can be 'table' or 'text' for custom queries
        "tdsx_table": original table name in the tdsx file
        "table": SQL-ready table name (schema.table)
        "name": name of the table relation (probably just the table name) or 'Custom SQL Query'
        "query_text": optional, text of the Custom SQL Query
        "connection": id of the connection to the table (found elsewhere in the tdsx file)
        "table_key": key of the table in DynamoDB
        "id": id of the table relation in the tdsx file
        "extract_table_name": name of the table in the extract file
    }
    editor's note: This is too large! Make it a class!
    """
    tree = ET.parse(tds_file_path)
    root = tree.getroot()
    objects = root.findall(".//object")
    # Each object that we care about (eg a Redshift -> Extract linkage)
    # should have two relations, one for Redshift, one for the extract
    tables = []
    for obj in objects:
        relations = obj.findall(".//relation")
        logger.info(f"Relations: {relations}")

        if len(relations) == 1 and "connection" in relations[0].attrib.keys():
            connection_str = relations[0].attrib["connection"]
            # TODO: There is a better way to do this based off of the expected connections
            # use the class attribute of the connection
            if any(sub in connection_str for sub in ["googledrive", "excel"]):
                logger.info(
                    f"Skipping obj {obj} because it is a Google Drive or Excel file"
                )
                continue

        # Find the redshift relation
        redshift_relations = [
            relation
            for relation in relations
            if "connection" in relation.attrib.keys()
            and "redshift" in relation.attrib["connection"]
        ]
        if len(redshift_relations) != 1:
            # We don't expect
            raise ValueError(
                f"Expected 1 redshift relation, found {len(redshift_relations)}: {redshift_relations}"
            )
        redshift_relation = redshift_relations[0]
        # Find the extract relation
        extract_relations = [
            relation
            for relation in relations
            if "name" in relation.attrib.keys()
            and relation.getparent().get("context") == "extract"
        ]
        if len(extract_relations) != 1:
            raise ValueError(
                f"Expected 1 extract relation, found {len(extract_relations)}: {extract_relations}"
            )
        extract_relation = extract_relations[0]
        table = redshift_relation.attrib

        # Old functionality, needs to be cleaned up
        if "table" in table.keys():
            table["tdsx_table"] = table.get("table")
            table["table"] = table.get("table").replace("[", "").replace("]", "")

        if table["name"] == "Custom SQL Query":
            table["query_text"] = redshift_relation.text
            table["table_key"] = get_custom_sql_query_table_key(
                table.get("name"), table.get("query_text")
            )
        else:
            table["table_key"] = table.get("table")

        # THIS is the table's unique ID in the tds file. Need to use this
        table["id"] = obj.get("id")
        # Get the actual table's name in the hyper file
        table["extract_table_name"] = extract_relation.get("table")
        tables.append(table)
    logger.info(f"Tables: {tables}")
    return tables


def extract_tds_from_tdsx(tdsx_path: str, extract_dir: str) -> str:
    """Extract .tdsx archive and return path to .tds file."""
    with zipfile.ZipFile(Path(tdsx_path).resolve(), "r") as zip_ref:
        zip_ref.extractall(extract_dir)
    # Locate the .tds file
    tds_file_path = None
    for root, _, files in os.walk(extract_dir):
        for file in files:
            if file.endswith(".tds"):
                tds_file_path = os.path.join(root, file)
                break
        if tds_file_path:
            break

    if not tds_file_path:
        raise FileNotFoundError("No .tds file found within the .tdsx archive.")

    return tds_file_path


def get_expected_hyper_path_from_tds(
    tds_path: str,
    extracted_root: str,
) -> str:
    """
    Given a .tds file extracted from a .tdsx and the extraction root,
    return the expected Hyper extract file path based on the Hyper connection dbname.

    Assumes exactly one Hyper connection.
    """

    tds_file = Path(tds_path)
    extracted_directory = Path(extracted_root)

    logger.info(f"TDS path: {tds_file}")
    logger.info(f"Extracted root: {extracted_directory}")

    xml = tds_file.read_text(encoding="utf-8", errors="replace")

    # Find Hyper connection dbname(s)
    dbnames = re.findall(
        r"<connection[^>]+class=['\"]hyper['\"][^>]+dbname=['\"]([^'\"]+)['\"]",
        xml,
        flags=re.IGNORECASE,
    )

    if not dbnames:
        raise ValueError("No Hyper connection found in .tds file")

    if len(dbnames) > 1:
        raise ValueError(
            f"Multiple Hyper connections found ({dbnames}); "
            "function assumes exactly one"
        )

    dbname = dbnames[0]

    # expected_hyper_path = extracted_root / "Data" / "Extracts" / f"{dbname}.hyper"
    expected_hyper_path = extracted_directory / dbname
    logger.info(f"Expected Hyper path: {expected_hyper_path}")

    return str(expected_hyper_path)


def find_hyper_file(extract_dir: str) -> Optional[str]:
    """Find .hyper file in extracted directory. Returns path or None."""
    for root, _, files in os.walk(extract_dir):
        for file in files:
            if file.endswith(".hyper"):
                return os.path.join(root, file)
    return None


def get_datetime_str() -> str:
    """Get current datetime as string for S3 keys."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%SZ")


def normalize_s3_name(
    name: str,
    *,
    lowercase: bool = True,
    max_length: int = 128,
) -> str:
    """
    Normalize a string (e.g. Tableau datasource or workbook name)
    into a safe S3 key / filename component.

    Rules:
    - Convert unicode → ASCII
    - Lowercase (optional)
    - Replace spaces and separators with '_'
    - Remove all characters except [a-z0-9._-]
    - Collapse repeated separators
    - Trim leading/trailing separators
    - Enforce max length
    """
    if not name:
        raise ValueError("Name for thing in S3 is required")

    # Normalize unicode (é → e, etc.)
    value = unicodedata.normalize("NFKD", name)
    value = value.encode("ascii", "ignore").decode("ascii")

    if lowercase:
        value = value.lower()

    # Replace spaces and common separators with underscore
    value = re.sub(r"[ \t\n/\\:]+", "_", value)

    # Remove anything unsafe
    value = re.sub(r"[^a-z0-9._-]", "", value)

    # Collapse multiple underscores/dashes
    value = re.sub(r"[_-]{2,}", "_", value)

    # Trim
    value = value.strip("._-")

    # Enforce max length
    if len(value) > max_length:
        value = value[:max_length].rstrip("._-")

    return value


def create_tdsx_from_directory(directory: str, output_path: str):
    """Create a .tdsx file from a directory."""
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rootdir, _, files in os.walk(directory):
            for f in files:
                full = os.path.join(rootdir, f)
                arc = os.path.relpath(full, directory)
                z.write(full, arc)
    logger.info(f"Created {output_path} from {directory}")
