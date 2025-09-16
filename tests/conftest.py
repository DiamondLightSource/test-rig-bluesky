from pathlib import Path

import pytest
from bluesky import RunEngine
from dodal.common.beamlines.beamline_utils import set_path_provider
from ophyd_async.core import StaticPathProvider, UUIDFilenameProvider


@pytest.fixture(scope="function")
def run_engine():
    run_engine = RunEngine(call_returns_result=True)
    yield run_engine
    if run_engine.state not in ("idle", "panicked"):
        run_engine.halt()


@pytest.fixture(scope="session", autouse=True)
def path_provider() -> None:
    provider = StaticPathProvider(UUIDFilenameProvider(), Path("/tmp"))
    set_path_provider(provider)
