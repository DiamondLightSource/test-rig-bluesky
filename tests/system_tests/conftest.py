import os
import socket
import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest
from blueapi.client.client import BlueapiClient
from blueapi.config import ApplicationConfig, ConfigLoader
from bluesky_stomp.messaging import StompClient
from bluesky_stomp.models import Broker

from test_rig_bluesky.testing import BlueskyPlanRunner

PROJECT_ROOT = Path(__file__).parent.parent.parent

BEAMLINE_HOSTS = [
    "b01-1-ws001.diamond.ac.uk",
    "b01-1-control.diamond.ac.uk",
    "bl01c-ea-serv-01.diamond.ac.uk",
    "bl01c-di-serv-01.diamond.ac.uk",
]


def pytest_configure(config: pytest.Config):
    config.addinivalue_line(
        "markers", "control_system: test requires direct access to the control system"
    )


def pytest_runtest_setup(item: pytest.Item):
    if next(item.iter_markers(name="control_system"), None) is not None:
        if not on_controllable_machine():
            pytest.skip(
                reason=textwrap.dedent(f"""
                    This test needs direct access to the control system and can only
                    be run from one of the following machines: {BEAMLINE_HOSTS}
                """)
            )


def on_controllable_machine() -> bool:
    hostname = socket.gethostname()
    return hostname in BEAMLINE_HOSTS


@pytest.fixture
def instrument() -> str:
    return os.environ.get("INSTRUMENT", os.environ.get("BEAMLINE", "b01-1"))


@pytest.fixture
def latest_commissioning_instrument_session() -> str:
    # Hardcoding this until a suitable API comes along
    return "cm40661-1"


@pytest.fixture
def data_directory(
    instrument: str, latest_commissioning_instrument_session: str
) -> Path:
    # Should retrieve this info from numtracker
    return (
        Path("/dls")
        / instrument
        / "data"
        / "2025"
        / latest_commissioning_instrument_session
    )


@pytest.fixture
def config() -> ApplicationConfig:
    loader = ConfigLoader(ApplicationConfig)
    loader.use_values_from_yaml(
        PROJECT_ROOT / "configuration" / "b01-1-blueapi-client.yaml"
    )
    return loader.load()


@pytest.fixture
def client(config: ApplicationConfig) -> BlueapiClient:
    return BlueapiClient.from_config(config)


@pytest.fixture
def stomp_client(config: ApplicationConfig) -> Generator[StompClient]:
    assert config.stomp.url.host is not None
    assert config.stomp.url.port is not None

    client = StompClient.for_broker(
        broker=Broker(
            host=config.stomp.url.host,
            port=config.stomp.url.port,
            auth=config.stomp.auth,
        )
    )
    client.connect()
    yield client
    client.disconnect()


@pytest.fixture
def bluesky_plan_runner(
    client: BlueapiClient,
    stomp_client: StompClient,
    data_directory: Path,
) -> BlueskyPlanRunner:
    return BlueskyPlanRunner(client, stomp_client)
