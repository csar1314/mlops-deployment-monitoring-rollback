import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings
from src.engine import Engine


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += seconds


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    service = Engine(
        Settings(tmp_path, token="test-secret", demo_enabled=True, controller_enabled=False), clock=clock
    )
    try:
        yield service
    finally:
        service.close()
