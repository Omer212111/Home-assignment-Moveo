import pytest

from app.settings import settings


@pytest.fixture(autouse=True)
def _trajectories_to_tmp(tmp_path, monkeypatch):
    """Tests must not write chat trajectories into data/."""
    monkeypatch.setattr(settings, "trajectory_dir", tmp_path / "trajectories")
