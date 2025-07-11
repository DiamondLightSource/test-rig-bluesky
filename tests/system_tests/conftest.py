import os
from collections.abc import Generator
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest
from blueapi.client.client import BlueapiClient
from blueapi.config import ApplicationConfig, ConfigLoader
from blueapi.service.model import TaskRequest
from bluesky_stomp.messaging import MessageContext, StompClient
from bluesky_stomp.models import Broker, MessageTopic

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


class BlueskyPlanRunner:
    def __init__(
        self, client: BlueapiClient, stomp_client: StompClient, instrument_session: str
    ):
        self.client = client
        self.stomp_client = stomp_client
        self.instrument_session = instrument_session

    def run(
        self, plan: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> None:
        params = params or {}

        # Listen for NeXus file events, see below
        nexus_finished_message = Future()

        def on_nexus_message(message: dict[str, Any], _: MessageContext) -> None:
            if message["status"] == "FINISHED":
                nexus_finished_message.set_result(message)

        self.stomp_client.subscribe(
            MessageTopic(name="gda.messages.scan"), on_nexus_message
        )

        # Run plan
        end_event = self.client.run_task(
            TaskRequest(
                name=plan, params=params, instrument_session=self.instrument_session
            ),
            timeout=timeout,
        )
        assert end_event.task_status is not None
        task_id = end_event.task_status.task_id

        # Check task ran and did not error
        task = self.client.get_task(task_id)
        assert task.is_complete
        assert len(task.errors) == 0

        # Search for a new NeXus file event, with numtracker
        # we will be able to programmatically correlate the
        # file with the plan, see
        # https://jira.diamond.ac.uk/browse/DCS-194
        nexus_finished_message.result(timeout=timeout)


@pytest.fixture
def bluesky_plan_runner(
    client: BlueapiClient,
    stomp_client: StompClient,
    latest_commissioning_instrument_session: str,
    data_directory: Path,
) -> BlueskyPlanRunner:
    return BlueskyPlanRunner(
        client, stomp_client, latest_commissioning_instrument_session
    )
