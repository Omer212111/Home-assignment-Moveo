import pytest

from app.settings import settings


@pytest.fixture(scope="session", autouse=True)
def _db_in_tmp(tmp_path_factory):
    """Tests use their own SQLite file, so scoring runs never land in data/weather_risk.db."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "db_path", tmp_path_factory.mktemp("db") / "test.db")
        yield


@pytest.fixture(autouse=True)
def _trajectories_to_tmp(tmp_path, monkeypatch):
    """Tests must not write chat trajectories into data/."""
    monkeypatch.setattr(settings, "trajectory_dir", tmp_path / "trajectories")
