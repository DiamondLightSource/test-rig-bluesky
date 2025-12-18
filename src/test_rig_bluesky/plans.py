import math
from pathlib import Path
from typing import Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.plans import count
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.devices.motors import XYZStage
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from dodal.plans import spec_scan
from ophyd_async.core import (
    DetectorTrigger,
    Device,
    Settings,
    SettingsProvider,
    StandardFlyer,
    TriggerInfo,
    YamlSettingsProvider,
)
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.adcore import (
    NDAttributeDataType,
    NDAttributeParam,
)
from ophyd_async.epics.adcore._core_io import NDROIStatNIO
from ophyd_async.epics.pmac import (
    PmacIO,
    PmacTrajectoryTriggerLogic,
)
from ophyd_async.fastcs.panda import (
    HDFPanda,
    ScanSpecInfo,
    ScanSpecSeqTableTriggerLogic,
    SeqBlock,
)
from ophyd_async.plan_stubs import (
    apply_settings,
    apply_settings_if_different,
    retrieve_settings,
    setup_ndattributes,
    store_settings,
)
from scanspec.specs import Fly, Line, Spec

imaging_detector = inject("imaging_detector")
spectroscopy_detector = inject("spectroscopy_detector")
sample_stage = inject("sample_stage")
pmac = inject("pmac")
pandabox = inject("pandabox")


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
            "fileio-nd_array_port",
            "fileio-enable_callbacks",
            "driver-acquire",
            "driver-trigger_mode",
            "driver-trigger_source",
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
            "roistat-nd_array_port",
            "roistat-enable_callbacks",
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
                addr=channel - 1,
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


def spectroscopy_fly(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabox: HDFPanda = pandabox,
    spec: Spec[Movable] | None = None,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
):
    # Prepare motor info using trajectory scanning
    scan_frame_duration = 0.1
    num_x = 2
    num_y = 100
    spec = spec or Fly(
        scan_frame_duration
        @ (Line(sample_stage.y, 0, 5, num_x) * ~Line(sample_stage.x, 0, 1, num_y))  # type: ignore
    )

    detector_deadtime = 2e-3 * 1.01
    total = num_x * num_y

    trigger_logic = spec
    pmac_trajectory = PmacTrajectoryTriggerLogic(pmac)
    pmac_trajectory_flyer = StandardFlyer(pmac_trajectory)
    table: SeqBlock = pandabox.seq.__1  # type: ignore # noqa: SLF001

    scan_spec_info = ScanSpecInfo(spec=spec, deadtime=detector_deadtime)

    panda_trigger_logic = StandardFlyer(
        ScanSpecSeqTableTriggerLogic(
            table,
        )
    )

    scan_frame_livetime = scan_frame_duration - detector_deadtime

    # Prepare Panda file writer trigger info
    panda_hdf_info = TriggerInfo(
        number_of_events=total,
        trigger=DetectorTrigger.CONSTANT_GATE,
        livetime=scan_frame_livetime,
        deadtime=detector_deadtime,
    )

    # Prepare Panda file writer trigger info
    detector_info = TriggerInfo(
        number_of_events=total,
        trigger=DetectorTrigger.CONSTANT_GATE,
        livetime=scan_frame_livetime,
        deadtime=detector_deadtime,
    )

    @attach_data_session_metadata_decorator()
    @bpp.run_decorator()
    @bpp.stage_decorator(
        [pandabox, panda_trigger_logic, spectroscopy_detector, pmac_trajectory_flyer]
    )
    def inner_plan():
        # create a group that is waited for
        # Hashable prepare_group = ["pmac_trajectory_flyer", "trigger_logic"]

        # Prepare pmac with the trajectory
        yield from bps.prepare(pmac_trajectory_flyer, trigger_logic)
        # prepare sequencer table
        yield from bps.prepare(panda_trigger_logic, scan_spec_info)
        # prepare panda and hdf writer once, at start of scan
        yield from bps.prepare(pandabox, panda_hdf_info)
        # prepare spectroscopy_detector and info
        # waiting for this last prepare means all prepare functions will be complete
        yield from bps.prepare(spectroscopy_detector, detector_info, wait=True)

        # Need to run this after detectors are prepared
        yield from bps.declare_stream(
            pandabox, spectroscopy_detector, name="primary", collect=True
        )

        # Start the detectors and hdf writers acquiring.
        # Configure the panda (seq table triggering).
        # create a group that is waited for before pmac_trajectory_flyer kicked off on
        # its own with wait=true.
        yield from bps.kickoff(pandabox)
        yield from bps.kickoff(panda_trigger_logic)
        yield from bps.kickoff(spectroscopy_detector, wait=True)

        # Start the trajectory.
        yield from bps.kickoff(pmac_trajectory_flyer, wait=True)

        # Wait for the scan to complete whilst continuously collecting the data.
        yield from bps.collect_while_completing(
            flyers=(
                pmac_trajectory_flyer,
                panda_trigger_logic,
                pandabox,
                spectroscopy_detector,
            ),
            dets=(
                pandabox,
                spectroscopy_detector,
            ),
            stream_name="primary",
        )

    yield from inner_plan()


def demo_spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabox: HDFPanda = pandabox,
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
    if True:
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
    else:
        yield from spectroscopy_fly(
            spectroscopy_detector=spectroscopy_detector,
            sample_stage=sample_stage,
            pmac=pmac,
            pandabox=pandabox,
            spec=None,
            exposure_time=exposure_time,
            metadata=metadata,
        )
