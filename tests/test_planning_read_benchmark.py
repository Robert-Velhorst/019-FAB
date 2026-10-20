from pathlib import Path

import pytest

from scripts.benchmark_planning_reads import parse_args, run


@pytest.mark.parametrize("arguments", [
    ["--documents", "0"], ["--documents", "5001"], ["--samples", "0"],
    ["--samples", "11"], ["--batch-limit", "501"], ["--payload-bytes", "65537"],
    ["--documents", "5000", "--payload-bytes", "65536"],
])
def test_benchmark_rejects_unbounded_work_before_creating_data(arguments):
    with pytest.raises(SystemExit):
        parse_args(arguments)


def test_benchmark_uses_fresh_data_ignores_operator_paths_and_compares_results(tmp_path, monkeypatch):
    sentinel = tmp_path / "operator.sqlite3"
    sentinel.write_bytes(b"preserve operator evidence")
    monkeypatch.setenv("FAB_LOCAL_LEDGER_PATH", str(sentinel))
    monkeypatch.setenv("FAB_LOCAL_BACKUP_DIR", str(tmp_path / "operator-backups"))
    result = run(parse_args(["--documents", "5", "--payload-bytes", "1024", "--samples", "1"]))
    assert result["syntheticOnly"] and result["temporaryDataRemoved"] and not result["providerWrites"]
    assert result["boundedCounters"]["equivalent"]
    assert result["duplicateGraphProjection"]["equivalent"]
    assert sentinel.read_bytes() == b"preserve operator evidence"
    assert not (tmp_path / "operator-backups").exists()
    assert str(Path(tmp_path)) not in str(result)
