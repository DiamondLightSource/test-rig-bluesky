import asyncio
import unittest.mock
from collections import defaultdict

import pytest
from bluesky import RunEngine
from dodal.beamlines.b01_1 import imaging_detector, sample_stage, spectroscopy_detector
from dodal.devices.motors import XYZStage
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.testing import assert_emitted, callback_on_mock_put, set_mock_value
from scanspec.specs import Line

from test_rig_bluesky.plans import demo_spectroscopy, snapshot, spectroscopy


@pytest.fixture
def _imaging_detector() -> AravisDetector:
    det = imaging_detector(connect_immediately=True, mock=True)
    _mock_detector_behavior(det)
    return det


@pytest.fixture
def _spectroscopy_detector() -> AravisDetector:
    det = spectroscopy_detector(connect_immediately=True, mock=True)
    _mock_detector_behavior(det)
    return det


@pytest.fixture
def _sample_stage() -> XYZStage:
    stage = sample_stage(connect_immediately=True, mock=True)

    set_mock_value(stage.x.low_limit_travel, -10.0)
    set_mock_value(stage.x.high_limit_travel, 10.0)
    set_mock_value(stage.y.low_limit_travel, -10.0)
    set_mock_value(stage.y.high_limit_travel, 10.0)

    set_mock_value(stage.x.velocity, 1.0)
    set_mock_value(stage.y.velocity, 1.0)

    return stage


def _mock_detector_behavior(detector: AravisDetector) -> None:
    async def mock_acquisition() -> None:
        # Get number of images to capture per acquire
        num_images = await detector.driver.num_images.get_value()
        set_mock_value(detector.fileio.num_capture, num_images)

        # Increment from current num captured to new value
        current_num_captured = await detector.fileio.num_captured.get_value()
        for i in range(current_num_captured, current_num_captured + num_images + 1):
            set_mock_value(detector.fileio.num_captured, i)

    async def on_acquire(acquire: bool, wait: bool) -> None:
        if acquire:
            asyncio.create_task(mock_acquisition())

    set_mock_value(detector.fileio.file_path_exists, True)
    callback_on_mock_put(detector.driver.acquire, on_acquire)


def test_snapshot(
    RE: RunEngine,
    _imaging_detector: AravisDetector,
    _spectroscopy_detector: AravisDetector,
    _sample_stage: XYZStage,
):
    docs = defaultdict(list)
    RE.subscribe(lambda name, doc: docs[name].append(doc))

    RE(snapshot(_imaging_detector, _spectroscopy_detector, _sample_stage))

    assert_emitted(
        docs, start=1, descriptor=1, stream_resource=2, stream_datum=2, event=1, stop=1
    )
    assert docs["stream_resource"][0].get("data_key") == "imaging_detector"
    assert docs["stream_resource"][1].get("data_key") == "spectroscopy_detector"
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }


async def test_spectroscopy(
    RE: RunEngine,
    _spectroscopy_detector: AravisDetector,
    _sample_stage: XYZStage,
):
    docs = defaultdict(list)
    RE.subscribe(lambda name, doc: docs[name].append(doc))

    RE(
        spectroscopy(
            _spectroscopy_detector,
            _sample_stage,
            Line(_sample_stage.y, 4.2, 6, 3) * Line(_sample_stage.x, 0, 5, 10),
            0.2,
        )
    )

    assert await _spectroscopy_detector.driver.acquire_time.get_value() == 0.2

    assert_emitted(
        docs,
        start=1,
        descriptor=1,
        stream_resource=4,
        stream_datum=4 * 30,
        event=30,
        stop=1,
    )
    assert docs["stream_resource"][0].get("data_key") == "spectroscopy_detector"
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }


async def test_spectroscopy_defaults(
    RE: RunEngine,
    _spectroscopy_detector: AravisDetector,
    _sample_stage: XYZStage,
):
    docs = defaultdict(list)
    RE.subscribe(lambda name, doc: docs[name].append(doc))

    RE(spectroscopy(_spectroscopy_detector, _sample_stage))

    assert await _spectroscopy_detector.driver.acquire_time.get_value() == 0.1

    assert_emitted(
        docs,
        start=1,
        descriptor=1,
        stream_resource=4,
        stream_datum=4 * 5,
        event=5,
        stop=1,
    )
    assert docs["stream_resource"][0].get("data_key") == "spectroscopy_detector"
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }


def test_spectroscopy_prepares_and_waits_before_doing_anything_else(RE: RunEngine):
    plan = spectroscopy()
    message_1, message_2 = next(plan), next(plan)

    assert message_1.command == "prepare"
    assert message_2.command == "wait"


def test_demo_spectroscopy():
    fake_detector = unittest.mock.MagicMock(name="fake_detector")
    fake_stage = unittest.mock.MagicMock(name="fake_stage")
    with unittest.mock.patch("test_rig_bluesky.plans.spectroscopy") as mock_spec:
        # Call the generator function and exhaust it
        generator = demo_spectroscopy(
            spectroscopy_detector=fake_detector,
            sample_stage=fake_stage,
            total_number_of_scan_points=25,
        )

        # Consume the generator so that the spectroscopy call is made
        for _ in generator:
            pass

    mock_spec.assert_called_once()
    called_kwargs = mock_spec.call_args.kwargs
    assert called_kwargs["spectroscopy_detector"] is fake_detector
    assert called_kwargs["sample_stage"] is fake_stage
    assert called_kwargs["spec"] == Line(fake_stage.y, 0.0, 5.0, 5) * Line(
        fake_stage.x, 0.0, 5.0, 5
    )
