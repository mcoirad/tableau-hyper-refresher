"""Shared runtime settings and retry policy."""

from __future__ import annotations

import os
from enum import IntEnum

from tenacity import stop_after_attempt, wait_exponential

RETRY_KWARGS = {
    "reraise": True,
    "stop": stop_after_attempt(5),
    "wait": wait_exponential(multiplier=30, min=1, max=300),
}


class ColumnMetadataField(IntEnum):
    """Column order returned by Redshift metadata queries."""

    COLUMN_NAME = 0
    DATA_TYPE = 1
    UDT_NAME = 2
    IS_NULLABLE = 3
    NUMERIC_PRECISION = 4
    NUMERIC_SCALE = 5
    CHARACTER_MAXIMUM_LENGTH = 6

    @classmethod
    def signature_field_count(cls) -> int:
        return cls.NUMERIC_SCALE + 1


def get_env(key: str, default: str | None = None) -> str:
    """Return an environment variable or fail with a useful message."""

    value = os.environ.get(key, default)
    if value is None:
        raise ValueError(f"Environment variable {key} must be set")
    return value
