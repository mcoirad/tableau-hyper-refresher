"""S3 artifact storage operations."""

from __future__ import annotations

import logging
import os
from typing import Optional

import boto3
from tenacity import retry

from .settings import RETRY_KWARGS, get_env
from .tds import get_datetime_str, normalize_s3_name

logger = logging.getLogger(__name__)


class S3Client:
    """Wrapper for S3 operations."""

    def __init__(self):
        self.aws_region = get_env("AWS_REGION", "us-west-2")
        self.s3 = boto3.client("s3", region_name=self.aws_region)
        self.expiring_bucket = get_env("TABLEAU_HYPER_REFRESHER_BUCKET")
        self.retained_bucket = get_env("TABLEAU_HYPER_REFRESHER_RETAINED_BUCKET")

    @retry(**RETRY_KWARGS)
    def upload_file(self, local_path: str, bucket: str, key: str):
        """Upload a file to S3."""
        self.s3.upload_file(local_path, bucket, key)
        logger.info(f"Uploaded {local_path} to s3://{bucket}/{key}")

    @retry(**RETRY_KWARGS)
    def download_file(self, bucket: str, key: str, local_path: str):
        """Download a file from S3."""
        os.makedirs(
            os.path.dirname(local_path) if os.path.dirname(local_path) else ".",
            exist_ok=True,
        )
        self.s3.download_file(bucket, key, local_path)
        logger.info(f"Downloaded s3://{bucket}/{key} to {local_path}")

    @retry(**RETRY_KWARGS)
    def download_folder(self, bucket: str, prefix: str, local_dir: str):
        """Download all files from an S3 prefix to a local directory."""
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel = key[len(prefix) :].lstrip("/")
                dest_path = os.path.join(local_dir, rel)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                self.s3.download_file(bucket, key, dest_path)
                logger.info(f"Downloaded {key} to {dest_path}")

    @retry(**RETRY_KWARGS)
    def upload_hyper_file(
        self,
        hyper_file_path: str,
        datasource_id: str,
        datetime_str: Optional[str] = None,
    ) -> str:
        """
        Upload a hyper file to S3 with standardized naming and path structure.

        Args:
            hyper_file_path: Local path to the hyper file
            datasource_id: Tableau datasource ID
            datetime_str: Optional datetime string for the S3 key (defaults to current datetime)

        Returns:
            S3 key where the file was uploaded
        """
        if datetime_str is None:
            datetime_str = get_datetime_str()

        hyper_filename = normalize_s3_name(os.path.basename(hyper_file_path))
        hyper_file_s3_key = f"{datasource_id}/{datetime_str}/{hyper_filename}"

        self.upload_file(
            local_path=hyper_file_path,
            bucket=self.expiring_bucket,
            key=hyper_file_s3_key,
        )

        logger.info(
            f"Uploaded hyper file to s3://{self.expiring_bucket}/{hyper_file_s3_key}"
        )
        return hyper_file_s3_key
