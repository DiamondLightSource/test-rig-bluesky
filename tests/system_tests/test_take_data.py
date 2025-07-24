from blueapi.service.model import TaskRequest

from test_rig_bluesky.testing import BlueskyPlanRunner


def test_collect_data(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
) -> None:
    bluesky_plan_runner.run(
        TaskRequest(
            name="count",
            params={"detectors": ["imaging_detector"], "num": 5},
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=10.0,
    )
