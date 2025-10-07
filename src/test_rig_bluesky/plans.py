import math
from pathlib import Path
from typing import Any

from bluesky import plan_stubs as bps
from bluesky.plans import count
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.devices.motors import XYZStage
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from dodal.plans import spec_scan
from ophyd_async.core import Device, Settings, SettingsProvider, YamlSettingsProvider
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.adcore import (
    NDAttributeDataType,
    NDAttributeParam,
)
from ophyd_async.epics.adcore._core_io import NDROIStatNIO
from ophyd_async.plan_stubs import (
    apply_settings,
    apply_settings_if_different,
    ensure_connected,
    retrieve_settings,
    setup_ndattributes,
    store_settings,
)
from scanspec.specs import Fly, Line, Spec

imaging_detector = inject("imaging_detector")
spectroscopy_detector = inject("spectroscopy_detector")
sample_stage = inject("sample_stage")


def save_settings(
    device: Device,
    design_name: str,
) -> MsgGenerator[None]:
    provider = _settings_provider()
    yield from store_settings(provider, design_name, device)


def load_settings(
    device: Device,
    design_name: str,
    whitelist_pvs: list[str] | None = None,
) -> MsgGenerator[None]:
    provider = _settings_provider()
    settings = yield from retrieve_settings(provider, design_name, device)
    if whitelist_pvs is None:
        settings_to_set = settings
    else:
        signal_values = {
            signal: value
            for signal, value in settings.items()
            if signal.name.replace(f"{device.name}-", "") in whitelist_pvs
        }
        settings_to_set = Settings(settings.device, signal_values)
    yield from apply_settings_if_different(settings_to_set, apply_settings)


def _settings_provider() -> SettingsProvider:
    this_directory = Path(__file__).parent
    return YamlSettingsProvider(this_directory)


@attach_data_session_metadata_decorator()
def snapshot(
    imaging_detector: AravisDetector = imaging_detector,
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
) -> MsgGenerator[None]:
    """Capture a snapshot of the current state of the beamline."""
    yield from count([imaging_detector, spectroscopy_detector, sample_stage])


def spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    spec: Spec[Movable] | None = None,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a spectroscopy scan."""
    yield from load_settings(
        device=spectroscopy_detector,
        design_name="spectroscopy_detector_baseline",
        whitelist_pvs=[
            "fileio.nd_array_port",
            "roistat-channels-array_counter",
            "roistat-channels-1-min_x",
            "roistat-channels-1-min_y",
            "roistat-channels-1-name_",
            "roistat-channels-1-size_x",
            "roistat-channels-1-size_y",
            "roistat-channels-1-use",
            "roistat-channels-2-min_x",
            "roistat-channels-2-min_y",
            "roistat-channels-2-name_",
            "roistat-channels-2-size_x",
            "roistat-channels-2-size_y",
            "roistat-channels-2-use",
            "roistat-channels-3-min_x",
            "roistat-channels-3-min_y",
            "roistat-channels-3-name_",
            "roistat-channels-3-size_x",
            "roistat-channels-3-size_y",
            "roistat-channels-3-use",
        ],
    )

    # We call mv instead of prepare because prepare cannot technically be used
    # outside of a run.
    # See: https://github.com/DiamondLightSource/blueapi/issues/1211
    #
    # Deadtime taken from
    # https://github.com/bluesky/ophyd-async/blob/15fa34b6ea2a28e2f27265a5564c9ee36423f1b7/src/ophyd_async/epics/adaravis/_aravis_controller.py#L11
    yield from bps.mv(
        *(spectroscopy_detector.driver.acquire_time, exposure_time),
        *(spectroscopy_detector.driver.acquire_period, exposure_time + 1961e-6),
        wait=True,
    )

    params: list[NDAttributeParam] = []
    for channel in list(spectroscopy_detector.roistat.channels.keys()):  # type: ignore
        roistatn = spectroscopy_detector.roistat.channels[channel]  # type: ignore
        assert isinstance(roistatn, NDROIStatNIO)

        channel_name = yield from bps.rd(roistatn.name_)

        params.append(
            NDAttributeParam(
                name=f"{channel_name}Total",
                param="ROISTAT_TOTAL",
                datatype=NDAttributeDataType.DOUBLE,
                addr=channel,
                description=f"Sum of {channel_name} channel",
            )
        )

    yield from setup_ndattributes(spectroscopy_detector.roistat, params)  # type: ignore

    yield from load_settings(
        device=sample_stage,
        design_name="sample_stage_baseline",
        whitelist_pvs=[
            "x-acceleration_time",
            "x-velocity",
            "y-acceleration_time",
            "y-velocity",
        ],
    )

    spec = spec or Line(sample_stage.x, 0, 5, 5)

    yield from spec_scan({spectroscopy_detector, sample_stage}, spec, metadata=metadata)


def demo_spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    total_number_of_scan_points: int = 25,
    grid_size: float = 5.0,
    grid_origin_x: float = 0.0,
    grid_origin_y: float = 0.0,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Spectroscopy plan intended for use in Visr demonstrations to visitors.
    The time taken to scan is approximately linear in total_numbers_of_grid_points.
    All other parameters can be left at their defaults.
    """
    xsteps = ysteps = int(round(math.sqrt(max(total_number_of_scan_points, 1))))
    xmin = grid_origin_x
    xmax = grid_origin_x + grid_size
    ymin = grid_origin_y
    ymax = grid_origin_y + grid_size
    grid = Line(sample_stage.y, ymin, ymax, ysteps) * Line(
        sample_stage.x, xmin, xmax, xsteps
    )
    yield from spectroscopy(
        spectroscopy_detector=spectroscopy_detector,
        sample_stage=sample_stage,
        spec=grid,
        exposure_time=exposure_time,
        metadata=metadata,
    )


from ophyd_async.core import StandardFlyer
from ophyd_async.epics.pmac import PmacTrajectoryTriggerLogic


def spectroscopy_flyscan():
    import bluesky.preprocessors as bpp
    from dodal.beamlines import b01_1

    stage = b01_1.sample_stage()
    pmac = b01_1.pmac()
    yield from ensure_connected(pmac, stage)

    # Prepare motor info using trajectory scanning
    spec = Fly(float(3) @ (Line(stage.y, 1, 2, 3) * ~Line(stage.x, 1, 5, 5)))
    pmac_trajectory = PmacTrajectoryTriggerLogic(pmac)
    pmac_trajectory_flyer = StandardFlyer(pmac_trajectory)

    @attach_data_session_metadata_decorator()
    @bpp.run_decorator()
    @bpp.stage_decorator([pmac])
    def inner_plan():
        # Prepare pmac with the trajectory
        yield from bps.prepare(pmac_trajectory_flyer, spec, wait=True)

        # kickoff devices waiting for all of them
        yield from bps.kickoff(pmac_trajectory_flyer, wait=True)

        yield from bps.complete_all(pmac_trajectory_flyer, wait=True)

    yield from inner_plan()
