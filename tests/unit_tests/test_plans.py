import asyncio
import unittest.mock
from collections import defaultdict
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

import dodal.beamlines.b01_1 as b01_1
import pytest
import yaml
from bluesky import RunEngine
from dodal.devices.beamlines.b01_1.diodes import RGBDiodes
from dodal.devices.current_amplifiers import EnzLocumRange
from dodal.devices.motors import XYZStage
from ophyd_async.core import (
    callback_on_mock_put,
    get_mock_put,
    set_mock_value,
)
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.adcore import NDFileHDF5IO
from ophyd_async.fastcs.panda import HDFPanda
from ophyd_async.testing import assert_emitted
from scanspec.specs import Line, Spec

import test_rig_bluesky
from test_rig_bluesky.plans import (
    ScanType,
    fly_scan,
    load_settings,
    save_settings,
    serialize_spec,
    snapshot,
)
from test_rig_bluesky.spectroscopy_plans import (
    PANDABOX_WHITELIST,
    demo_spectroscopy,
    spectroscopy,
)


@pytest.fixture
def spectroscopy_detector(run_engine: RunEngine) -> AravisDetector:
    det = b01_1.spectroscopy_detector.build(connect_immediately=True, mock=True)
    _mock_detector_behavior(det)
    return det


@pytest.fixture
def spectroscopy_stage(run_engine: RunEngine) -> XYZStage:
    stage = b01_1.spectroscopy_stage.build(connect_immediately=True, mock=True)

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


@pytest.fixture
def pandabox(run_engine: RunEngine) -> HDFPanda:
    return b01_1.pandabox.build(connect_immediately=True, mock=True)


@pytest.fixture
def spectroscopy_diodes(run_engine: RunEngine) -> RGBDiodes:
    return b01_1.spectroscopy_diodes.build(connect_immediately=True, mock=True)


def _mock_detector_behavior(detector: AravisDetector) -> None:
    hdf = detector.get_plugin("hdf", NDFileHDF5IO)

    async def mock_acquisition() -> None:
        # Get number of images to capture per acquire
        num_images = await detector.driver.num_images.get_value()
        set_mock_value(hdf.num_capture, num_images)

        # Increment from current num captured to new value
        current_num_captured = await hdf.num_captured.get_value()
        for i in range(current_num_captured, current_num_captured + num_images + 1):
            set_mock_value(hdf.num_captured, i)

    async def on_acquire(acquire: bool) -> None:
        if acquire:
            asyncio.create_task(mock_acquisition())

    set_mock_value(hdf.file_path_exists, True)
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
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(snapshot([spectroscopy_detector, spectroscopy_stage]))

    assert_emitted(
        docs, start=1, descriptor=1, stream_resource=1, stream_datum=1, event=1, stop=1
    )
    assert docs["stream_resource"][0].get("data_key") == "spectroscopy_detector"
    assert docs["event"][0]["data"] == {
        "spectroscopy_stage-x": 0.0,
        "spectroscopy_stage-y": 0.0,
        "spectroscopy_stage-z": 0.0,
    }


async def test_spectroscopy(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    pandabrick: HDFPanda,
    spectroscopy_diodes: RGBDiodes,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            spectroscopy_stage=spectroscopy_stage,
            pandabrick=pandabrick,
            spectroscopy_diodes=spectroscopy_diodes,
            spec=Line(spectroscopy_stage.y, 4.2, 6, 3)
            * Line(spectroscopy_stage.x, 0, 5, 10),
            exposure_time=0.2,
            scan_type=ScanType.STEP,
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
    spectroscopy_stage: XYZStage,
    pandabrick: HDFPanda,
    spectroscopy_diodes: RGBDiodes,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            spectroscopy_stage=spectroscopy_stage,
            pandabrick=pandabrick,
            spectroscopy_diodes=spectroscopy_diodes,
            scan_type=ScanType.STEP,
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
    spectroscopy_stage: XYZStage,
    pandabrick: HDFPanda,
    spectroscopy_diodes: RGBDiodes,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            spectroscopy_stage=spectroscopy_stage,
            pandabrick=pandabrick,
            spectroscopy_diodes=spectroscopy_diodes,
            scan_type=ScanType.STEP,
        )
    )

    data_keys = [resource.get("data_key") for resource in docs["stream_resource"]]
    assert data_keys == ["spectroscopy_detector", "BlueTotal", "GreenTotal", "RedTotal"]
    assert docs["event"][0]["data"] == {
        "spectroscopy_stage-x": 0.0,
        "spectroscopy_stage-y": 0.0,
        "spectroscopy_stage-z": 0.0,
        "spectroscopy_diodes-red": 0,
        "spectroscopy_diodes-green": 0,
        "spectroscopy_diodes-blue": 0,
    }


async def test_spectroscopy_sets_exposure_time_and_acquire_period(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    pandabrick: HDFPanda,
    spectroscopy_diodes: RGBDiodes,
):
    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            spectroscopy_stage=spectroscopy_stage,
            pandabrick=pandabrick,
            spectroscopy_diodes=spectroscopy_diodes,
            exposure_time=1.0,
            scan_type=ScanType.STEP,
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
            spectroscopy_stage=fake_stage,
            total_number_of_scan_points=25,
            scan_type=ScanType.FLY,
        )

        # Consume the generator so that the spectroscopy call is made
        for _ in generator:
            pass

    mock_spec.assert_called_once()
    called_kwargs = mock_spec.call_args.kwargs
    assert called_kwargs["spectroscopy_detector"] is fake_detector
    assert called_kwargs["spectroscopy_stage"] is fake_stage
    assert called_kwargs["spec"] == Line(fake_stage.y, 0.0, 5.0, 5) * ~Line(
        fake_stage.x, 0.0, 5.0, 5
    )
    assert called_kwargs["scan_type"] == ScanType.FLY


def test_serialize_spec_names_device_axes(spectroscopy_stage: XYZStage):
    spec = Line(spectroscopy_stage.y, 4.2, 6, 3) * Line(spectroscopy_stage.x, 0, 5, 10)

    serialized = serialize_spec(spec)

    assert serialized["outer"]["axis"] == spectroscopy_stage.y.name
    assert serialized["inner"]["axis"] == spectroscopy_stage.x.name
    assert Spec.deserialize(serialized).axes() == [
        spectroscopy_stage.y.name,
        spectroscopy_stage.x.name,
    ]


async def test_spectroscopy_sets_amplifier_range(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    pandabrick: HDFPanda,
    spectroscopy_diodes: RGBDiodes,
):
    docs = defaultdict(list)
    run_engine.subscribe(lambda name, doc: docs[name].append(doc))

    run_engine(
        spectroscopy(
            spectroscopy_detector=spectroscopy_detector,
            spectroscopy_stage=spectroscopy_stage,
            pandabrick=pandabrick,
            spectroscopy_diodes=spectroscopy_diodes,
            amplifier_range=EnzLocumRange.RANGE_10NA,
            scan_type=ScanType.STEP,
        )
    )

    get_mock_put(spectroscopy_diodes.amplifier.range).assert_called_once_with(
        EnzLocumRange.RANGE_10NA
    )
    configuration = docs["descriptor"][0]["configuration"]["spectroscopy_diodes"]
    assert "spectroscopy_diodes-amplifier-range_max" in configuration["data"]


def test_spectroscopy_rejects_auto_amplifier_range(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    spectroscopy_diodes: RGBDiodes,
):
    set_mock_value(spectroscopy_diodes.amplifier.range, EnzLocumRange.AUTO)

    with pytest.raises(ValueError, match="Auto"):
        run_engine(
            spectroscopy(
                spectroscopy_detector=spectroscopy_detector,
                spectroscopy_stage=spectroscopy_stage,
                spectroscopy_diodes=spectroscopy_diodes,
                scan_type=ScanType.STEP,
            )
        )


def test_fly_scan_rejects_pandabox_without_diodes(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    pandabox: HDFPanda,
):
    with pytest.raises(ValueError, match="pandabox was given without diodes"):
        run_engine(
            fly_scan(
                Line(spectroscopy_stage.x, 0, 5, 5),
                spectroscopy_detector,
                pandabox=pandabox,
            )
        )


def test_fly_scan_rejects_diodes_without_pandabox(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    spectroscopy_diodes: RGBDiodes,
):
    with pytest.raises(ValueError, match="diodes were given without pandabox"):
        run_engine(
            fly_scan(
                Line(spectroscopy_stage.x, 0, 5, 5),
                spectroscopy_detector,
                diodes=spectroscopy_diodes,
            )
        )


def test_spectroscopy_fly_scan_uses_pandabox_and_diodes(
    run_engine: RunEngine,
    spectroscopy_detector: AravisDetector,
    spectroscopy_stage: XYZStage,
    pandabrick: HDFPanda,
    pandabox: HDFPanda,
    spectroscopy_diodes: RGBDiodes,
):
    with (
        unittest.mock.patch(
            "test_rig_bluesky.spectroscopy_plans.load_panda_settings",
            return_value=iter(()),
        ) as mock_load_panda_settings,
        unittest.mock.patch(
            "test_rig_bluesky.spectroscopy_plans.fly_scan", return_value=iter(())
        ) as mock_fly_scan,
    ):
        run_engine(
            spectroscopy(
                spectroscopy_detector=spectroscopy_detector,
                spectroscopy_stage=spectroscopy_stage,
                pandabrick=pandabrick,
                pandabox=pandabox,
                spectroscopy_diodes=spectroscopy_diodes,
                scan_type=ScanType.FLY,
            )
        )

    mock_load_panda_settings.assert_called_once_with(
        panda=pandabox,
        design_name="pandabox_baseline",
        whitelist_pvs=PANDABOX_WHITELIST,
    )
    mock_fly_scan.assert_called_once()
    called_kwargs = mock_fly_scan.call_args.kwargs
    assert called_kwargs["pandabox"] is pandabox
    assert called_kwargs["diodes"] is spectroscopy_diodes


def test_pandabox_baseline_captures_diodes_under_their_names(
    spectroscopy_diodes: RGBDiodes,
):
    baseline = (
        Path(test_rig_bluesky.__file__).parent / "baselines/pandabox_baseline.yaml"
    )
    datasets = yaml.safe_load(baseline.read_text())

    assert datasets["fmc_in.val5_dataset"] == spectroscopy_diodes.red.name
    assert datasets["fmc_in.val6_dataset"] == spectroscopy_diodes.green.name
    assert datasets["fmc_in.val7_dataset"] == spectroscopy_diodes.blue.name
