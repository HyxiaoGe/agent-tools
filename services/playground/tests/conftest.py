import pytest


@pytest.fixture(autouse=True)
def isolated_quota_database(monkeypatch, tmp_path):
    # 每个测试使用自己的计数库，不污染真实预览的试用额度。
    monkeypatch.setenv("PLAYGROUND_QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
