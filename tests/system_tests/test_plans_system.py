from blueapi.service.model import TaskRequest
from bluesky import RunEngine
from dodal.beamlines.b01_1 import imaging_detector, sample_stage, spectroscopy_detector

from test_rig_bluesky.plans import snapshot, spectroscopy
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


def test_snapshot_re():
    RE = RunEngine()
    _imaging_detector = imaging_detector(connect_immediately=True)
    _spectroscopy_detector = spectroscopy_detector(connect_immediately=True)
    _sample_stage = sample_stage(connect_immediately=True)

    RE(snapshot(_imaging_detector, _spectroscopy_detector, _sample_stage))


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


def test_spectroscopy_re():
    RE = RunEngine()
    _spectroscopy_detector = spectroscopy_detector(connect_immediately=True)
    _sample_stage = sample_stage(connect_immediately=True)

    RE(spectroscopy(_spectroscopy_detector, _sample_stage))
