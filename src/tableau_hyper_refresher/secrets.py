"""AWS Secrets Manager integration."""

from __future__ import annotations

import json
from typing import Any

import boto3


def get_json_secret(secret_id: str, region_name: str) -> dict[str, Any]:
    """Load and decode a JSON secret without logging its value."""

    client = boto3.client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_id)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise ValueError(f"Secret {secret_id!r} does not contain SecretString JSON")
    value = json.loads(secret_string)
    if not isinstance(value, dict):
        raise ValueError(f"Secret {secret_id!r} must decode to a JSON object")
    return value
