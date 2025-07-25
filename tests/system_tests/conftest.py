import os
from collections.abc import Generator
from pathlib import Path

import pytest
from blueapi.client.client import BlueapiClient
from blueapi.config import ApplicationConfig, ConfigLoader
from bluesky_stomp.messaging import StompClient
from bluesky_stomp.models import Broker

from test_rig_bluesky.testing import BlueskyPlanRunner

PROJECT_ROOT = Path(__file__).parent.parent.parent


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
