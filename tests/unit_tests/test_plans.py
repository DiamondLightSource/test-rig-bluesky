import asyncio
from collections import defaultdict

from bluesky import RunEngine
from dodal.beamlines.b01_1 import oav, sample_det, sample_stage
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.testing import assert_emitted, callback_on_mock_put, set_mock_value
from scanspec.specs import Line

from test_rig_bluesky.plans import snapshot, spectroscopy


def mock_detector_behavior(detector: AravisDetector) -> None:
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


def test_snapshot(RE: RunEngine):
    docs = defaultdict(list)
    RE.subscribe(lambda name, doc: docs[name].append(doc))

    _sample_det = sample_det(connect_immediately=True, mock=True)
    mock_detector_behavior(_sample_det)

    _oav = oav(connect_immediately=True, mock=True)
    mock_detector_behavior(_oav)

    _sample_stage = sample_stage(connect_immediately=True, mock=True)

    RE(snapshot(_sample_det, _oav, _sample_stage))

    assert_emitted(
        docs, start=1, descriptor=1, stream_resource=2, stream_datum=2, event=1, stop=1
    )
    assert docs["stream_resource"][0].get("data_key") == "sample_det"
    assert docs["stream_resource"][1].get("data_key") == "oav"
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }


async def test_spectroscopy(RE: RunEngine):
    docs = defaultdict(list)
    RE.subscribe(lambda name, doc: docs[name].append(doc))

    _oav = oav(connect_immediately=True, mock=True)
    mock_detector_behavior(_oav)

    _sample_stage = sample_stage(connect_immediately=True, mock=True)
    set_mock_value(_sample_stage.x.velocity, 1.0)
    set_mock_value(_sample_stage.y.velocity, 1.0)

    RE(
        spectroscopy(
            _oav,
            _sample_stage,
            Line(_sample_stage.y, 4.2, 6, 3) * Line(_sample_stage.x, 0, 5, 10),
            0.2,
        )
    )

    assert await _oav.driver.acquire_time.get_value() == 0.2

    assert_emitted(
        docs,
        start=1,
        descriptor=1,
        stream_resource=1,
        stream_datum=30,
        event=30,
        stop=1,
    )
    assert docs["stream_resource"][0].get("data_key") == "oav"
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }


async def test_spectroscopy_defaults(RE: RunEngine):
    docs = defaultdict(list)
    RE.subscribe(lambda name, doc: docs[name].append(doc))

    _oav = oav(connect_immediately=True, mock=True)
    mock_detector_behavior(_oav)

    _sample_stage = sample_stage(connect_immediately=True, mock=True)
    set_mock_value(_sample_stage.x.velocity, 1.0)
    set_mock_value(_sample_stage.y.velocity, 1.0)

    RE(spectroscopy(_oav, _sample_stage))

    assert await _oav.driver.acquire_time.get_value() == 0.1

    assert_emitted(
        docs,
        start=1,
        descriptor=1,
        stream_resource=1,
        stream_datum=5,
        event=5,
        stop=1,
    )
    assert docs["stream_resource"][0].get("data_key") == "oav"
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }
