import asyncio
import unittest.mock
from collections import defaultdict
from unittest.mock import ANY, AsyncMock, Mock, patch

import dodal.beamlines.b01_1 as b01_1
import pytest
from bluesky import RunEngine
from dodal.devices.motors import XYZStage
from ophyd_async.core import callback_on_mock_put, set_mock_value
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.fastcs.panda import HDFPanda
from ophyd_async.testing import assert_emitted
from scanspec.specs import Line

from test_rig_bluesky.plans import (
    load_settings,
    save_settings,
    snapshot,
)
from test_rig_bluesky.spectroscopy_plans import demo_spectroscopy, spectroscopy


@pytest.fixture
def imaging_detector(run_engine: RunEngine) -> AravisDetector:
    det = b01_1.imaging_detector.build(connect_immediately=True, mock=True)
    _mock_detector_behavior(det)
    return det


@pytest.fixture
def spectroscopy_detector(run_engine: RunEngine) -> AravisDetector:
    det = b01_1.spectroscopy_detector.build(connect_immediately=True, mock=True)
    _mock_detector_behavior(det)
    return det


@pytest.fixture
def sample_stage(run_engine: RunEngine) -> XYZStage:
    stage = b01_1.sample_stage.build(connect_immediately=True, mock=True)

    set_mock_value(stage.x.low_limit_travel, -10.0)
    set_mock_value(stage.x.high_limit_travel, 10.0)
    set_mock_value(stage.y.low_limit_travel, -10.0)
    set_mock_value(stage.y.high_limit_travel, 10.0)

    set_mock_value(stage.x.velocity, 1.0)
    set_mock_value(stage.y.velocity, 1.0)

    return stage


@pytest.fixture
def pandabrick(run_engine: RunEngine) -> HDFPanda:
    return b01_1.pandabrick.build(connect_immediately=True, mock=True)


def _mock_detector_behavior(detector: AravisDetector) -> None:
    async def mock_acquisition() -> None:
        # Get number of images to capture per acquire
        num_images = await detector.driver.num_images.get_value()
        set_mock_value(detector.hdf.num_capture, num_images)

        # Increment from current num captured to new value
        current_num_captured = await detector.hdf.num_captured.get_value()
        for i in range(current_num_captured, current_num_captured + num_images + 1):
            set_mock_value(detector.hdf.num_captured, i)

    async def on_acquire(acquire: bool) -> None:
        if acquire:
            asyncio.create_task(mock_acquisition())

    set_mock_value(detector.hdf.file_path_exists, True)
    callback_on_mock_put(detector.driver.acquire, on_acquire)


@patch("test_rig_bluesky.plans.YamlSettingsProvider")
def test_save_setting(
    mock_provider: Mock,
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
):
    # Provider needs to be async or the RunEngine will complain
    mock_provider.return_value = AsyncMock()
    run_engine(save_settings(spectroscopy_detector, design_name="test"))
    mock_provider.return_value.store.assert_called_once_with("test", ANY)


async def test_load_subset_of_settings(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
):
    run_engine(
        load_settings(
            spectroscopy_detector,
            design_name="spectroscopy_detector_baseline",
            whitelist_pvs=["driver-acquire_time"],
        )
    )

    assert await spectroscopy_detector.driver.acquire_time.get_value() == 0.1
    assert await spectroscopy_detector.roistat.channels[1].min_x.get_value() == 0  # type:ignore


async def test_load_settings(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
):
    run_engine(
        load_settings(
            spectroscopy_detector,
            design_name="spectroscopy_detector_baseline",
        )
    )

    assert await spectroscopy_detector.driver.acquire_period.get_value() == 0.021815
    assert await spectroscopy_detector.driver.num_images.get_value() == 1
    assert await spectroscopy_detector.roistat.channels[1].min_x.get_value() == 295  # type:ignore


def test_snapshot(
    run_engine: RunEngine,
    imaging_detector: AravisDetector,
    spectroscopy_detector: AravisDetector,
    sample_stage: XYZStage,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(snapshot([imaging_detector, spectroscopy_detector, sample_stage]))

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
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    sample_stage: XYZStage,
    pandabrick: HDFPanda,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            sample_stage=sample_stage,
            pandabrick=pandabrick,
            spec=Line(sample_stage.y, 4.2, 6, 3) * Line(sample_stage.x, 0, 5, 10),
            exposure_time=0.2,
            fly=False,
        )
    )

    assert await spectroscopy_detector.driver.acquire_time.get_value() == 0.2

    assert_emitted(
        docs,
        start=1,
        descriptor=1,
        stream_resource=4,
        stream_datum=4 * 30,
        event=30,
        stop=1,
    )


async def test_spectroscopy_defaults(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    sample_stage: XYZStage,
    pandabrick: HDFPanda,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            sample_stage=sample_stage,
            pandabrick=pandabrick,
            fly=False,
        )
    )

    assert await spectroscopy_detector.driver.acquire_time.get_value() == 0.1

    assert_emitted(
        docs,
        start=1,
        descriptor=1,
        stream_resource=4,
        stream_datum=4 * 5,
        event=5,
        stop=1,
    )


def test_spectroscopy_datasets(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    sample_stage: XYZStage,
    pandabrick: HDFPanda,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            sample_stage=sample_stage,
            pandabrick=pandabrick,
            fly=False,
        )
    )

    data_keys = [resource.get("data_key") for resource in docs["stream_resource"]]
    assert data_keys == ["spectroscopy_detector", "BlueTotal", "GreenTotal", "RedTotal"]
    assert docs["event"][0]["data"] == {
        "sample_stage-x": 0.0,
        "sample_stage-y": 0.0,
        "sample_stage-z": 0.0,
    }


async def test_spectroscopy_sets_exposure_time_and_acquire_period(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    sample_stage: XYZStage,
    pandabrick: HDFPanda,
):
    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            sample_stage=sample_stage,
            pandabrick=pandabrick,
            exposure_time=1.0,
            fly=False,
        )
    )
    assert await spectroscopy_detector.driver.acquire_time.get_value() == 1.0
    assert (
        await spectroscopy_detector.driver.acquire_period.get_value() == 1.0 + 1961e-6
    )


def test_demo_spectroscopy():
    fake_detector = unittest.mock.MagicMock(name="fake_detector")
    fake_stage = unittest.mock.MagicMock(name="fake_stage")
    with unittest.mock.patch(
        "test_rig_bluesky.spectroscopy_plans.spectroscopy"
    ) as mock_spec:
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
    assert called_kwargs["spec"] == Line(fake_stage.y, 0.0, 5.0, 5) * ~Line(
        fake_stage.x, 0.0, 5.0, 5
    )
