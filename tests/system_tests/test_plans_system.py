from blueapi.service.model import TaskRequest

from test_rig_bluesky.testing import BlueskyPlanRunner


def test_snapshot(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
):
    events = bluesky_plan_runner.run(
        TaskRequest(
            name="snapshot",
            params={},
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=10,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [1]


def test_spectroscopy(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
):
    events = bluesky_plan_runner.run(
        TaskRequest(
            name="spectroscopy",
            params={},
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=10,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [5]
