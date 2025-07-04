import asyncio
from collections import defaultdict

from bluesky import RunEngine
from dodal.beamlines.b01_1 import oav, sample_det, sample_stage
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.testing import assert_emitted, callback_on_mock_put, set_mock_value

from test_rig_bluesky.plans import snapshot


def mock_detector_behavior(detector: AravisDetector) -> None:
    async def mock_acquisition() -> None:
        num_images = await detector.driver.num_images.get_value()
        set_mock_value(detector.fileio.num_capture, num_images)

        for i in range(1, num_images + 1):
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
