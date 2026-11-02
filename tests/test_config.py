import pytest

from src.config import Settings
from src.engine import Engine


@pytest.mark.parametrize("empty_file", [False, True])
def test_missing_or_empty_token_is_random_and_persisted(tmp_path, empty_file):
    if empty_file:
        (tmp_path / "admin-token.txt").write_text("\n  ")
    first = Settings(tmp_path).ensure_token()
    assert len(first) >= 32
    assert Settings(tmp_path).ensure_token() == first


def test_rejected_second_instance_cannot_change_running_service_token(engine):
    original = (engine.settings.data_dir / "admin-token.txt").read_text()
    other = Settings(engine.settings.data_dir, token="a-different-admin-token", controller_enabled=False)
    with pytest.raises(RuntimeError):
        Engine(other)
    assert (engine.settings.data_dir / "admin-token.txt").read_text() == original
