from pathlib import Path
from zipfile import ZipFile

import pytest

from tableau_hyper_refresher.tds import (
    create_tdsx_from_directory,
    extract_tds_from_tdsx,
    get_custom_sql_query_table_key,
    get_expected_hyper_path_from_tds,
    normalize_s3_name,
)


def test_custom_sql_key_is_deterministic() -> None:
    first = get_custom_sql_query_table_key("Custom SQL Query", "select 1")
    second = get_custom_sql_query_table_key("Custom SQL Query", "select 1")
    assert first == second
    assert first.startswith("Custom SQL Query_")


def test_tdsx_round_trip_and_hyper_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "example.tds").write_text(
        '<datasource><connection class="hyper" dbname="Data/Extracts/example.hyper" /></datasource>'
    )
    extracts = source / "Data" / "Extracts"
    extracts.mkdir(parents=True)
    (extracts / "example.hyper").write_bytes(b"fixture")
    archive = tmp_path / "example.tdsx"

    create_tdsx_from_directory(str(source), str(archive))
    with ZipFile(archive) as zipped:
        assert "example.tds" in zipped.namelist()

    destination = tmp_path / "expanded"
    tds_path = extract_tds_from_tdsx(str(archive), str(destination))
    assert Path(tds_path).name == "example.tds"
    assert get_expected_hyper_path_from_tds(tds_path, str(destination)).endswith(
        "Data/Extracts/example.hyper"
    )


def test_multiple_hyper_connections_are_rejected(tmp_path: Path) -> None:
    tds = tmp_path / "example.tds"
    tds.write_text(
        '<datasource><connection class="hyper" dbname="one.hyper" />'
        '<connection class="hyper" dbname="two.hyper" /></datasource>'
    )
    with pytest.raises(ValueError, match="Multiple Hyper connections"):
        get_expected_hyper_path_from_tds(str(tds), str(tmp_path))


def test_s3_name_normalization() -> None:
    assert normalize_s3_name("Résumé / Report.hyper") == "resume_report.hyper"
