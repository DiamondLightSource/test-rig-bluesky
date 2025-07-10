from collections import defaultdict
from concurrent.futures import Future
from typing import Any

from blueapi.client.client import BlueapiClient
from blueapi.service.model import TaskRequest
from bluesky_stomp.messaging import MessageContext, StompClient
from bluesky_stomp.models import MessageTopic


class BlueskyPlanRunner:
    def __init__(self, client: BlueapiClient, stomp_client: StompClient):
        self.client = client
        self.stomp_client = stomp_client

    def run(
        self, task_request: TaskRequest, timeout: float
    ) -> dict[str, list[dict[str, Any]]]:
        # Listen for NeXus file events, see below
        nexus_finished_message = Future()

        def on_nexus_message(message: dict[str, Any], _: MessageContext) -> None:
            if message["status"] == "FINISHED":
                nexus_finished_message.set_result(message)

        self.stomp_client.subscribe(
            MessageTopic(name="gda.messages.scan"), on_nexus_message
        )

        # Collect all events for the caller
        events = defaultdict(list)

        def collect(message: dict[str, Any], _: MessageContext):
            events[message["status"]].append(message)

        self.stomp_client.subscribe(MessageTopic(name="gda.messages.scan"), collect)

        # Run plan
        end_event = self.client.run_task(task_request, timeout=timeout)
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

        return events
