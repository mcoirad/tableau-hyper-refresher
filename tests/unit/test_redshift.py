from typing import Any

from tableau_hyper_refresher.redshift import RedshiftClient


def _client(monkeypatch, env: dict[str, str]) -> RedshiftClient:
    for name in (
        "REDSHIFT_SERVERLESS_WORKGROUP",
        "REDSHIFT_CLUSTER_ID",
        "REDSHIFT_SECRET_ARN",
        "REDSHIFT_DB_USER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REDSHIFT_DB", "warehouse")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "tableau_hyper_refresher.redshift.boto3.client",
        lambda *args, **kwargs: object(),
    )
    return RedshiftClient()


def test_serverless_data_api_configuration(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {
            "REDSHIFT_SERVERLESS_WORKGROUP": "analytics",
            "REDSHIFT_SECRET_ARN": "secret-reference",
        },
    )
    assert client._base_exec_kwargs() == {
        "Database": "warehouse",
        "WorkgroupName": "analytics",
        "SecretArn": "secret-reference",
    }


def test_cluster_data_api_configuration(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {"REDSHIFT_CLUSTER_ID": "warehouse-cluster", "REDSHIFT_DB_USER": "loader"},
    )
    assert client._base_exec_kwargs() == {
        "Database": "warehouse",
        "ClusterIdentifier": "warehouse-cluster",
        "DbUser": "loader",
    }


def test_redshift_to_hyper_type_mapping() -> None:
    client: Any = object.__new__(RedshiftClient)
    assert client._format_data_type("bigint", None, None, None, None) == "BIGINT"
    assert client._format_data_type("varchar", None, None, None, 128) == "VARCHAR(128)"
    assert client._format_data_type("numeric", None, 18, 4, None) == "NUMERIC(18,4)"
    assert client._format_data_type("numeric", None, 38, 4, None) == "DOUBLE PRECISION"
    assert (
        client._format_data_type("timestamp with time zone", None, None, None, None)
        == "TIMESTAMP WITH TIME ZONE"
    )
