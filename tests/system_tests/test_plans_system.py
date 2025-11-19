import dodal.beamlines.b01_1 as b01_1
import pytest
from blueapi.service.model import TaskRequest
from bluesky import RunEngine
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
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=20,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [5]


def test_spectroscopy_with_custom_trajectory(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
):
    scan_spec = Line("sample_stage.x", 0.0, 5.0, 5) * Line(
        "sample_stage.y", 2.0, 5.0, 3
    )
    events = bluesky_plan_runner.run(
        TaskRequest(
            name="spectroscopy",
            params={"spec": scan_spec.serialize()},
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=60,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [5, 3]


def test_demo_spectroscopy(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
):
    events = bluesky_plan_runner.run(
        TaskRequest(
            name="demo_spectroscopy",
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=1000,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [5, 5]


def test_demo_spectroscopy_with_custom_trajectory(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
):
    events = bluesky_plan_runner.run(
        TaskRequest(
            name="demo_spectroscopy",
            params={
                "exposure_time": 0.05,
                "grid_size": 2.5,
                "grid_origin_y": 0.5,
            },
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=40,
    )
    assert events["FINISHED"][0]["scanDimensions"] == [5, 5]


def test_generic_count(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
) -> None:
    bluesky_plan_runner.run(
        TaskRequest(
            name="count",
            params={
                "detectors": ["imaging_detector", "spectroscopy_detector"],
                "num": 5,
            },
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=10.0,
    )


def test_generic_scan(
    bluesky_plan_runner: BlueskyPlanRunner, latest_commissioning_instrument_session: str
) -> None:
    scan_spec = Line("sample_stage.x", 0.0, 5.0, 2) * Line(
        "sample_stage.y", 2.0, 5.0, 2
    )
    bluesky_plan_runner.run(
        TaskRequest(
            name="spec_scan",
            params={
                "detectors": ["imaging_detector", "spectroscopy_detector"],
                "spec": scan_spec.serialize(),
            },
            instrument_session=latest_commissioning_instrument_session,
        ),
        timeout=40.0,
    )


@pytest.mark.control_system
def test_spectroscopy_re():
    run_engine = RunEngine()
    spectroscopy_detector = b01_1.spectroscopy_detector(connect_immediately=True)
    sample_stage = b01_1.sample_stage(connect_immediately=True)

    scan_spec = Line(sample_stage.y, 0, 5, 50) * Line(sample_stage.x, 2, 5, 30)

    run_engine(spectroscopy(spectroscopy_detector, sample_stage, scan_spec))
