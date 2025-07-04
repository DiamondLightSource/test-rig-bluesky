from concurrent.futures import Future
from pathlib import Path
from typing import Any

from blueapi.client.client import BlueapiClient
from blueapi.service.model import TaskRequest
from bluesky_stomp.messaging import MessageContext, StompClient
from bluesky_stomp.models import MessageTopic


def test_collect_data(
    client: BlueapiClient,
    stomp_client: StompClient,
    latest_commissioning_instrument_session: str,
    data_directory: Path,
) -> None:
    # Listen for NeXus file events, see below
    nexus_finished_message = Future()

    def on_nexus_message(message: dict[str, Any], _: MessageContext) -> None:
        if message["status"] == "FINISHED":
            nexus_finished_message.set_result(message)

    stomp_client.subscribe(MessageTopic(name="gda.messages.scan"), on_nexus_message)

    # Run plan
    end_event = client.run_task(
        TaskRequest(
            name="count",
            params={"detectors": ["sample_det"], "num": 5},
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=10.0,
    )
    assert end_event.task_status is not None
    task_id = end_event.task_status.task_id

    # Check task ran and did not error
    task = client.get_task(task_id)
    assert task.is_complete
    assert len(task.errors) == 0

    # Search for a new NeXus file event, with numtracker
    # we will be able to programmatically correlate the
    # file with the plan, see
    # https://jira.diamond.ac.uk/browse/DCS-194
    nexus_finished_message.result(timeout=10.0)
