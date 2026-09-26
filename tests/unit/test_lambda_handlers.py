from dataclasses import dataclass

from tableau_hyper_refresher import lambda_handlers


@dataclass
class _Datasource:
    id: str
    name: str
    tags: list[str]


class _Tableau:
    def get_filtered_datasources(self, tags, tag_mode):
        assert tags == ["refresh-me"]
        assert tag_mode == "whitelist"
        return [_Datasource("ds-1", "Example", ["prod"])]

    def get_datasource(self, datasource_id):
        assert datasource_id == "ds-1"
        return _Datasource("ds-1", "Example", ["prod"])


def test_discover_uses_configurable_tags(monkeypatch) -> None:
    monkeypatch.setenv("TABLEAU_REFRESH_TAG", "refresh-me")
    monkeypatch.setenv("TABLEAU_PRODUCTION_TAG", "prod")
    monkeypatch.setattr(lambda_handlers, "TableauClient", _Tableau)

    result = lambda_handlers.discover_datasources({}, None)

    assert result["datasources"] == ["ds-1"]
    assert result["datasource_items"] == [
        {"id": "ds-1", "name": "Example", "is_production": True}
    ]


def test_summary_only_reports_production_failures() -> None:
    event = {
        "refresh_results": [
            {"name": "Production", "status": "failed", "is_production": True},
            {"name": "Sandbox", "status": "failed", "is_production": False},
            {"name": "Healthy", "status": "succeeded", "is_production": True},
        ]
    }

    result = lambda_handlers.summarize_refresh_results(event)

    assert result["FailedProductionDatasources"] == ["Production"]
    assert "Production" in result["FailureMessage"]
    assert "Sandbox" not in result["FailureMessage"]


class _Dynamo:
    def get_datasources_by_execution_id(self, execution_id):
        assert execution_id == "execution-1"
        return [
            {
                "tableau_datasource_id": "ds-2",
                "tables": [
                    {
                        "table_key": "analytics.events",
                        "table": "analytics.events",
                        "type": "table",
                    }
                ],
            },
            {
                "tableau_datasource_id": "ds-1",
                "tables": [
                    {
                        "table_key": "analytics.events",
                        "table": "analytics.events",
                        "type": "table",
                    }
                ],
            },
        ]


def test_unload_plan_deduplicates_shared_tables(monkeypatch) -> None:
    monkeypatch.setattr(lambda_handlers, "DynamoDBClient", _Dynamo)

    result = lambda_handlers.create_unload_plan(
        {"STEP_FUNCTION_EXECUTION_ID": "execution-1"}, None
    )

    assert result["tables"] == [
        {
            "table_text": "analytics.events",
            "type": "table",
            "table_key": "analytics.events",
            "datasource_ids": ["ds-1", "ds-2"],
            "datasource_ids_csv": "ds-1,ds-2",
        }
    ]
