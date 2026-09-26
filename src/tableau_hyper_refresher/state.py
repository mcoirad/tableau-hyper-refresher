"""DynamoDB workflow state operations."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import boto3
from tenacity import retry

from .settings import RETRY_KWARGS, get_env

logger = logging.getLogger(__name__)


class DynamoDBClient:
    """Wrapper for DynamoDB operations."""

    def __init__(self):
        self.aws_region = get_env("AWS_REGION", "us-west-2")
        self.dynamodb = boto3.resource("dynamodb", region_name=self.aws_region)
        self.datasources_table = get_env("TABLEAU_DATASOURCES_TABLE")
        self.datasource_tables_table = get_env("TABLEAU_DATASOURCE_TABLES_TABLE")
        self.tables_table = get_env("TABLEAU_TABLES_TABLE")

    @retry(**RETRY_KWARGS)
    def read_item(self, table_name: str, key: Dict) -> Optional[Dict]:
        """
        Read an item from a DynamoDB table by key.

        Args:
            table_name: Name of the DynamoDB table
            key: Dictionary containing the key attributes (e.g., {'id': 'value'} or {'pk': 'value', 'sk': 'value'})

        Returns:
            Dictionary containing the item attributes, or None if item not found
        """
        table = self.dynamodb.Table(table_name)
        try:
            response = table.get_item(Key=key)
            if "Item" in response:
                return response["Item"]
            return None
        except Exception as e:
            logger.info(f"Error reading item from {table_name}: {e}")
            raise

    @retry(**RETRY_KWARGS)
    def update_item(
        self,
        table_name: str,
        key: Dict,
        values: Dict,
    ) -> Dict:
        """
        Update an item in a DynamoDB table using a standard SET update expression.
        Automatically generates the update expression from the values dictionary.

        Args:
            table_name: Name of the DynamoDB table
            key: Dictionary containing the key attributes (e.g., {'id': 'value'} or {'pk': 'value', 'sk': 'value'})
            values: Dictionary of attribute names and values to update (e.g., {'attr1': 'value1', 'attr2': 123})

        Returns:
            Dictionary containing the updated item attributes
        """
        if not values:
            raise ValueError("values dictionary cannot be empty")

        table = self.dynamodb.Table(table_name)

        # DynamoDB reserved words that require expression attribute names
        # Common reserved words that could conflict in update expressions
        reserved_words = {
            "set",
            "remove",
            "add",
            "delete",
            "path",
            "data",
            "index",
            "key",
            "table",
            "type",
            "value",
            "values",
            "name",
            "names",
            "item",
            "items",
            "expression",
            "attribute",
            "attributes",
        }

        # Generate expression attribute names for reserved words and special characters
        expr_attr_names: dict[str, str] = {}
        expr_attr_values: dict[str, Any] = {}
        set_parts = []

        for attr_name, attr_value in values.items():
            # Use placeholder if attribute name is a reserved word or contains problematic chars
            # (underscores and hyphens are allowed, but spaces, dots, etc. are not)
            if attr_name.lower() in reserved_words or not all(
                c.isalnum() or c in "_-" for c in attr_name
            ):
                placeholder = f"#attr_{len(expr_attr_names)}"
                expr_attr_names[placeholder] = attr_name
                attr_placeholder = placeholder
            else:
                attr_placeholder = attr_name

            val_placeholder = f":val_{len(expr_attr_values)}"
            set_parts.append(f"{attr_placeholder} = {val_placeholder}")
            expr_attr_values[val_placeholder] = attr_value

        update_expression = f"SET {', '.join(set_parts)}"

        kwargs = {
            "Key": key,
            "UpdateExpression": update_expression,
            "ExpressionAttributeValues": expr_attr_values,
            "ReturnValues": "ALL_NEW",
        }

        if expr_attr_names:
            kwargs["ExpressionAttributeNames"] = expr_attr_names

        try:
            response = table.update_item(**kwargs)
            return response.get("Attributes", {})
        except Exception as e:
            logger.info(f"Error updating item in {table_name}: {e}")
            raise

    def delete_items_by_attribute(
        self,
        table_name: str,
        attribute_name: str,
        attribute_value: Any,
    ) -> int:
        """
        Delete items from a DynamoDB table that match a specific attribute value.
        Scans the table to find matching items, then deletes each by its key.

        Args:
            table_name: Name of the DynamoDB table
            attribute_name: Name of the attribute to filter on
            attribute_value: Value to match for deletion

        Returns:
            Number of items deleted
        """
        table = self.dynamodb.Table(table_name)

        # Get table key schema to know which attributes are keys
        try:
            table_desc = table.meta.client.describe_table(TableName=table_name)
            key_schema = table_desc["Table"]["KeySchema"]
            key_attributes = {key["AttributeName"] for key in key_schema}
        except Exception as e:
            logger.info(f"Error getting table schema for {table_name}: {e}")
            raise

        # Build filter expression
        # Handle reserved words and special characters for attribute name
        reserved_words = {
            "set",
            "remove",
            "add",
            "delete",
            "path",
            "data",
            "index",
            "key",
            "table",
            "type",
            "value",
            "values",
            "name",
            "names",
            "item",
            "items",
            "expression",
            "attribute",
            "attributes",
        }

        if attribute_name.lower() in reserved_words or not all(
            c.isalnum() or c in "_-" for c in attribute_name
        ):
            attr_placeholder = "#attr_name"
            filter_expression = f"{attr_placeholder} = :attr_value"
            expression_attribute_names = {attr_placeholder: attribute_name}
        else:
            filter_expression = f"{attribute_name} = :attr_value"
            expression_attribute_names = None

        expression_attribute_values = {":attr_value": attribute_value}

        # Scan table with filter and paginate through results
        deleted_count = 0
        scan_kwargs = {
            "FilterExpression": filter_expression,
            "ExpressionAttributeValues": expression_attribute_values,
        }

        if expression_attribute_names:
            scan_kwargs["ExpressionAttributeNames"] = expression_attribute_names

        while True:
            response = table.scan(**scan_kwargs)

            # Delete each matching item by its key
            for item in response.get("Items", []):
                # Extract key attributes from the item
                key = {attr: item[attr] for attr in key_attributes if attr in item}

                if key:
                    try:
                        table.delete_item(Key=key)
                        deleted_count += 1
                    except Exception as e:
                        logger.info(f"Error deleting item with key {key}: {e}")
                        raise

            # Check if there are more items to scan
            if "LastEvaluatedKey" not in response:
                break

            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        logger.info(
            f"Deleted {deleted_count} item(s) from {table_name} where {attribute_name} = {attribute_value}"
        )
        return deleted_count

    @retry(**RETRY_KWARGS)
    def scan(
        self,
        table_name: str,
        filter_expression: Optional[str] = None,
        expression_attribute_values: Optional[Dict] = None,
        expression_attribute_names: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        Scan a DynamoDB table and return all items matching the filter expression.
        Handles pagination automatically.

        Args:
            table_name: Name of the DynamoDB table
            filter_expression: Optional filter expression (e.g., "attribute_name = :value")
            expression_attribute_values: Optional dictionary of expression attribute values (e.g., {":value": "some_value"})
            expression_attribute_names: Optional dictionary of expression attribute names (for reserved words)

        Returns:
            List of dictionaries containing the items
        """
        table = self.dynamodb.Table(table_name)

        items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {}

        if filter_expression:
            scan_kwargs["FilterExpression"] = filter_expression

        if expression_attribute_values:
            scan_kwargs["ExpressionAttributeValues"] = expression_attribute_values

        if expression_attribute_names:
            scan_kwargs["ExpressionAttributeNames"] = expression_attribute_names

        # Paginate through all results
        while True:
            try:
                response = table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))

                # Check if there are more items to scan
                if "LastEvaluatedKey" not in response:
                    break

                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            except Exception as e:
                logger.info(f"Error scanning table {table_name}: {e}")
                raise

        logger.info(f"Scanned {len(items)} item(s) from {table_name}")
        return items

    def update_tableau_datasources(self, datasource_id: str, values: Dict):
        """Update a tableau datasource in DynamoDB."""
        response = self.update_item(
            table_name=self.datasources_table,
            key={"tableau_datasource_id": datasource_id},
            values=values,
        )
        return response

    def update_tableau_datasource_tables(
        self, datasource_id: str, table: str, values: Dict
    ):
        """Update a tableau datasource table in DynamoDB."""
        response = self.update_item(
            table_name=self.datasource_tables_table,
            key={"tableau_datasource_id": datasource_id, "table_id": table},
            values=values,
        )
        return response

    def get_tableau_datasource_table(
        self, datasource_id: str, table_id: str
    ) -> Optional[Dict]:
        """Get a single datasource-table mapping row from DynamoDB."""
        response = self.read_item(
            table_name=self.datasource_tables_table,
            key={"tableau_datasource_id": datasource_id, "table_id": table_id},
        )
        return response

    def get_tableau_datasource(self, datasource_id: str) -> Optional[Dict]:
        """Get a tableau datasource from DynamoDB."""
        response = self.read_item(
            table_name=self.datasources_table,
            key={"tableau_datasource_id": datasource_id},
        )
        return response

    def get_datasources_by_execution_id(self, execution_id: str) -> List[Dict]:
        """Get all tables from a DynamoDB execution."""
        response = self.scan(
            table_name=self.datasources_table,
            filter_expression="last_seen_by_sf_execution_id = :execution_id",
            expression_attribute_values={":execution_id": execution_id},
        )
        return response

    def update_tableau_table(self, table_id: str, values: Dict):
        """Update a tableau table in DynamoDB.
        Args:
            table_id: The ID of the tableau table (schema.table)
            values: The values to update
        """
        response = self.update_item(
            table_name=self.tables_table,
            key={"table_id": table_id},
            values=values,
        )
        return response

    def get_tableau_table(self, table_id: str) -> Optional[Dict]:
        """Get a tableau table from DynamoDB."""
        response = self.read_item(
            table_name=self.tables_table, key={"table_id": table_id}
        )
        return response
