from types import SimpleNamespace

from tableau_hyper_refresher import refresh, storage


def test_s3_client_resolves_both_bucket_classes(monkeypatch) -> None:
    monkeypatch.setenv("TABLEAU_HYPER_REFRESHER_BUCKET", "work-bucket")
    monkeypatch.setenv("TABLEAU_HYPER_REFRESHER_RETAINED_BUCKET", "retained-bucket")
    monkeypatch.setattr(storage.boto3, "client", lambda *args, **kwargs: object())

    client = storage.S3Client()

    assert client.expiring_bucket == "work-bucket"
    assert client.retained_bucket == "retained-bucket"


def test_hyper_file_uses_transient_bucket(monkeypatch) -> None:
    uploads = []
    monkeypatch.setenv("TABLEAU_HYPER_REFRESHER_BUCKET", "work-bucket")
    monkeypatch.setenv("TABLEAU_HYPER_REFRESHER_RETAINED_BUCKET", "retained-bucket")
    monkeypatch.setattr(storage.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(storage, "get_datetime_str", lambda: "2026_01_01_00_00_00")
    client = storage.S3Client()
    monkeypatch.setattr(
        client,
        "upload_file",
        lambda local_path, bucket, key: uploads.append((local_path, bucket, key)),
    )

    key = client.upload_hyper_file("/tmp/output.hyper", "datasource-1")

    assert key == "datasource-1/2026_01_01_00_00_00/output.hyper"
    assert uploads == [
        (
            "/tmp/output.hyper",
            "work-bucket",
            "datasource-1/2026_01_01_00_00_00/output.hyper",
        )
    ]


def test_refreshed_tdsx_uses_retained_bucket() -> None:
    uploads = []

    class _Storage:
        retained_bucket = "retained-bucket"

        def upload_hyper_file(self, hyper_file_path, datasource_id):
            return f"{datasource_id}/output.hyper"

        def upload_file(self, local_path, bucket, key):
            uploads.append((local_path, bucket, key))

    job = refresh.RefreshJob.__new__(refresh.RefreshJob)
    job.s3_client = _Storage()
    job.args = SimpleNamespace(datasource_id="datasource-1")
    job.new_hyper_path = "/tmp/output.hyper"
    job.tdsx_output = "/tmp/refreshed.tdsx"
    job.new_hyper_file_s3_key = None

    job._upload_outputs()

    assert uploads == [
        (
            "/tmp/refreshed.tdsx",
            "retained-bucket",
            "datasource-1/refreshed_datasource.tdsx",
        )
    ]


def test_incremental_watermark_advances_after_publish_step() -> None:
    updates = []

    class _State:
        def update_tableau_table(self, table_id, values):
            updates.append(("table", table_id, values))

        def update_tableau_datasource_tables(self, datasource_id, table_id, values):
            updates.append(("datasource", datasource_id, table_id, values))

    job = refresh.RefreshJob.__new__(refresh.RefreshJob)
    job.dynamo_client = _State()
    job.args = SimpleNamespace(datasource_id="datasource-1")
    job.tables_info = {
        "analytics.events": {
            "table_id": "analytics.events",
            "max_sort_column_value": "2026-01-03T00:00:00Z",
        }
    }
    from tableau_hyper_refresher.refresh_config import (
        IncrementalConfig,
        TableRefreshConfig,
    )

    job.resolved_table_configs = {
        "analytics.events": TableRefreshConfig(
            table_id="analytics.events",
            mode="upsert",
            primary_key=["event_id"],
            sort_column="updated_at",
            incremental=IncrementalConfig(enabled=True),
        )
    }

    job._advance_table_watermarks()

    assert updates[0][0:2] == ("table", "analytics.events")
    assert updates[1][0:3] == ("datasource", "datasource-1", "analytics.events")
