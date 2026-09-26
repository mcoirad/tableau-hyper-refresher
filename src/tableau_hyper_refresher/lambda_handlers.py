"""AWS Lambda handlers used by the Step Functions workflow."""

from __future__ import annotations

import logging
import os
from typing import Any

from .state import DynamoDBClient
from .tableau_client import TableauClient

logger = logging.getLogger(__name__)


def _has_tag(datasource: Any, tag: str) -> bool:
    tags = {value.lower() for value in (getattr(datasource, "tags", None) or [])}
    return tag.lower() in tags


def _datasource_item(client: TableauClient, datasource_id: str) -> dict[str, Any]:
    datasource = client.get_datasource(datasource_id)
    production_tag = os.getenv("TABLEAU_PRODUCTION_TAG", "production")
    return {
        "id": datasource.id,
        "name": datasource.name,
        "is_production": _has_tag(datasource, production_tag),
    }


def summarize_refresh_results(event: dict[str, Any]) -> dict[str, Any]:
    """Attach a stable summary of failed production refreshes."""

    failures: list[str] = []
    for result in event.get("refresh_results") or []:
        if not isinstance(result, dict):
            continue
        if result.get("status") != "failed" or not result.get("is_production"):
            continue
        name = result.get("name") or result.get("datasource_id")
        if name and name not in failures:
            failures.append(name)

    event["FailedProductionDatasources"] = failures
    event["FailureMessage"] = (
        "Production Tableau Hyper refresh failures:\n"
        + "\n".join(f"- {name}" for name in failures)
        if failures
        else None
    )
    return event


def discover_datasources(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Discover tagged Tableau datasources or summarize Map results."""

    del context
    if event.get("action") == "summarize_refresh_results":
        return summarize_refresh_results(event)

    client = TableauClient()
    provided_ids = event.get("datasources")
    if provided_ids and all(isinstance(value, str) for value in provided_ids):
        items = [
            _datasource_item(client, datasource_id) for datasource_id in provided_ids
        ]
    else:
        selector_tag = os.getenv("TABLEAU_REFRESH_TAG", "use_hyper_refresh")
        datasources = client.get_filtered_datasources(
            tags=[selector_tag], tag_mode="whitelist"
        )
        if not datasources:
            raise ValueError(f"No datasources found with tag {selector_tag!r}")
        items = [_datasource_item(client, datasource.id) for datasource in datasources]

    event["datasources"] = [item["id"] for item in items]
    event["datasource_items"] = items
    logger.info("Discovered %s Tableau datasource(s)", len(items))
    return event


def create_unload_plan(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Create one Redshift unload item per unique source table."""

    del context
    execution_id = event.get("STEP_FUNCTION_EXECUTION_ID") or os.getenv(
        "STEP_FUNCTION_EXECUTION_ID"
    )
    if not execution_id:
        raise ValueError("STEP_FUNCTION_EXECUTION_ID is required")

    datasources = DynamoDBClient().get_datasources_by_execution_id(execution_id)
    tables_by_key: dict[str, dict[str, Any]] = {}
    for datasource in datasources:
        datasource_id = datasource.get("tableau_datasource_id")
        if not datasource_id:
            continue
        for table in datasource.get("tables", []):
            table_key = table.get("table_key")
            if not table_key:
                continue
            planned = tables_by_key.setdefault(
                table_key,
                {
                    "table_text": table.get("query_text", table.get("table")),
                    "type": table.get("type"),
                    "table_key": table_key,
                    "datasource_ids": [],
                },
            )
            planned["datasource_ids"].append(datasource_id)

    tables = []
    for planned in tables_by_key.values():
        datasource_ids = sorted(set(planned["datasource_ids"]))
        planned["datasource_ids"] = datasource_ids
        planned["datasource_ids_csv"] = ",".join(datasource_ids)
        tables.append(planned)
    event["tables"] = tables
    logger.info("Planned %s unique Redshift table(s)", len(tables))
    return event
