import pytest
from blueapi.service.model import TaskRequest
from bluesky import RunEngine
from dodal.beamlines.b01_1 import sample_stage, spectroscopy_detector
from scanspec.specs import Line

from test_rig_bluesky.plans import spectroscopy
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


def test_demo_spectroscopy(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
):
    _sample_stage = sample_stage()
    scan_spec = Line(_sample_stage.y, 0, 5, 50) * Line(_sample_stage.x, 2, 5, 30)
    events = bluesky_plan_runner.run(
        TaskRequest(
            name="demo_spectroscopy",
            params={"spec": scan_spec.serialize()},
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=10,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [5, 5]


@pytest.mark.control_system
def test_spectroscopy_re():
    RE = RunEngine()
    _spectroscopy_detector = spectroscopy_detector(connect_immediately=True)
    _sample_stage = sample_stage(connect_immediately=True)

    scan_spec = Line(_sample_stage.y, 0, 5, 50) * Line(_sample_stage.x, 2, 5, 30)

    RE(spectroscopy(_spectroscopy_detector, _sample_stage, scan_spec))
