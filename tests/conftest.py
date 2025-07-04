import pytest
from bluesky import RunEngine


@pytest.fixture(scope="function")
def RE():
    RE = RunEngine(call_returns_result=True)
    yield RE
    if RE.state not in ("idle", "panicked"):
        RE.halt()
