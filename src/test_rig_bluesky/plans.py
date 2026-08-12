import logging
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
    TriggerInfo,
    YamlSettingsProvider,
)
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.adcore import (
    NDAttributeDataType,
    NDAttributeParam,
    NDROIStatNIO,
    setup_ndattributes,
)
from ophyd_async.epics.pmac import PmacIO, PmacScanInfo, PmacTrajectoryFlyableLogic
from ophyd_async.fastcs.panda import (
    HDFPanda,
    ScanSpecInfo,
    ScanSpecSeqTableFlyableLogic,
    SeqBlock,
    apply_panda_settings,
)
from ophyd_async.plan_stubs import (
    apply_settings,
    apply_settings_if_different,
    retrieve_settings,
    store_settings,
)
from scanspec.specs import Fly, Line, Spec

LOGGER = logging.getLogger(__name__)

# imaging_detector = inject("imaging_detector")
spectroscopy_detector = inject("spectroscopy_detector")
sample_stage = inject("sample_stage")
pmac = inject("pmac")
pandabrick = inject("pandabrick")
# pmac_trigger_logic = inject("pmac_trigger_logic")


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


def load_panda_settings(
    panda: HDFPanda,
    design_name: str,
    whitelist_pvs: list[str] | None = None,
) -> MsgGenerator[None]:
    provider = _settings_provider()
    settings = yield from retrieve_settings(provider, design_name, panda)
    if whitelist_pvs is None:
        settings_to_set = settings
    else:
        signal_values = {
            signal: value
            for signal, value in settings.items()
            if signal.name.replace(f"{panda.name}-", "") in whitelist_pvs
        }
        settings_to_set = Settings(settings.device, signal_values)
    yield from apply_settings_if_different(settings_to_set, apply_panda_settings)


def _settings_provider() -> SettingsProvider:
    this_directory = Path(__file__).parent
    return YamlSettingsProvider(this_directory)


@attach_data_session_metadata_decorator()
def snapshot(
    imaging_detector: AravisDetector,
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
) -> MsgGenerator[None]:
    """Capture a snapshot of the current state of the beamline."""
    yield from count([imaging_detector, spectroscopy_detector, sample_stage])


def spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    num_points: int = 25,
    spec: Spec[Movable] | None = None,
    exposure_time: float = 0.1,
    fly: bool = True,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a spectroscopy scan."""
    yield from load_settings(
        device=spectroscopy_detector,
        design_name="spectroscopy_detector_baseline",
        whitelist_pvs=[
            "hdf-nd_array_port",
            "hdf-enable_callbacks",
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
        group="spectroscopy_detector_aquire",
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

    # yield from load_panda_settings(
    #     panda=pandabrick,
    #     design_name="pandabrick_baseline",
    #     whitelist_pvs=["incenc-__3-val_dataset", "incenc-__2-val_dataset"],
    # )

    spec = spec or Line(sample_stage.x, 0, 5, 5)  # type: ignore

    # NOTE: replace with spec_serialized = spec.serialize() when this merges: https://github.com/bluesky/scanspec/pull/208
    from typing import Any

    from pydantic import TypeAdapter
    from scanspec.specs import Spec

    spec_serialzied = TypeAdapter(Spec[Any]).dump_python(
        spec, mode="json", fallback=repr
    )

    # write json-serializable form of spec into metadata
    if metadata is None:
        metadata = {}
    # spec.serialize() when https://github.com/bluesky/scanspec/pull/208 merges
    metadata["spec"] = spec_serialzied

    if fly:
        LOGGER.info("Performing a fly scan.")
        yield from fly_scan(
            spec,
            spectroscopy_detector,
            sample_stage,
            pmac,
            pandabrick,
            num_points,  # type: ignore
            exposure_time,
            metadata,
        )
    else:
        LOGGER.info("Performing a step scan.")
        yield from spec_scan(
            {spectroscopy_detector, sample_stage},
            spec,  # type: ignore
            metadata=metadata,
        )


def fly_scan(
    spec: Spec[Movable],
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    num_points: int = 1_000,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
):
    # yield from load_panda_settings(panda=pandabrick,design_name="pandabrick_baseline")

    yield from load_panda_settings(
        panda=pandabrick,
        design_name="pandabrick_baseline",
        whitelist_pvs=["incenc-3-val_dataset", "incenc-2-val_dataset"],
    )

    scan_frame_duration = exposure_time
    fly_spec = Fly(scan_frame_duration @ spec)  # type: ignore
    detector_deadtime = 2e-3 * 1.01

    table: SeqBlock = pandabrick.seq[1]  # type: ignore # noqa: SLF001

    pmac_scan_info = PmacScanInfo(spec=fly_spec, ramp_time=None, turnaround_time=None)  # type: ignore
    scan_spec_info = ScanSpecInfo(spec=fly_spec, deadtime=detector_deadtime)  # type: ignore

    # motor_pos_out = {
    #     sample_stage.x: PosOutScaleOffset.from_inenc(pandabrick, 2),
    #     sample_stage.y: PosOutScaleOffset.from_inenc(pandabrick, 3)
    # }

    # The trajectory and seq-table logics are bare FlyableLogic; wrap each in an
    # ephemeral StandardFlyable so the RunEngine can prepare/kickoff/complete it.
    pmac_trigger_logic = PmacTrajectoryFlyableLogic(pmac).with_device(
        name="pmac_trigger_logic"
    )
    panda_trigger_logic = ScanSpecSeqTableFlyableLogic(
        table,
        # motor_pos_out
    ).with_device(name="panda_trigger_logic")

    scan_frame_livetime = scan_frame_duration - detector_deadtime

    # Reproduce metadata present in spec_scan
    _md = {
        "plan_args": {
            "detectors": {det.name for det in [pandabrick, spectroscopy_detector]},
            "spec": repr(fly_spec),
        },
        "plan_name": "fly_scan",
        "shape": fly_spec.shape(),
        **(metadata or {}),
    }

    # Prepare Panda file writer trigger info
    panda_hdf_info = TriggerInfo(
        number_of_events=num_points,
        trigger=DetectorTrigger.EXTERNAL_LEVEL,
        livetime=scan_frame_livetime,
        deadtime=detector_deadtime,
    )

    # Prepare Panda file writer trigger info
    detector_info = TriggerInfo(
        number_of_events=num_points,
        trigger=DetectorTrigger.EXTERNAL_EDGE,
        livetime=scan_frame_livetime,
        deadtime=detector_deadtime,
    )

    @attach_data_session_metadata_decorator()
    @bpp.run_decorator(md=_md)
    @bpp.stage_decorator(
        [pandabrick, panda_trigger_logic, spectroscopy_detector, pmac_trigger_logic]
    )
    def inner_plan():
        # create a group that is waited for
        # Hashable prepare_group = ["pmac_trigger_logic", "trigger_logic"]

        # Prepare pmac with the trajectory
        yield from bps.prepare(pmac_trigger_logic, pmac_scan_info)
        # prepare sequencer table
        yield from bps.prepare(panda_trigger_logic, scan_spec_info)
        # prepare panda and hdf writer once, at start of scan
        yield from bps.prepare(pandabrick, panda_hdf_info)
        # prepare spectroscopy_detector and info
        # waiting for this last prepare means all prepare functions will be complete
        yield from bps.prepare(spectroscopy_detector, detector_info, wait=True)

        # Need to run this after detectors are prepared
        yield from bps.declare_stream(
            pandabrick, spectroscopy_detector, name="primary", collect=True
        )

        # Start the detectors and hdf writers acquiring.
        # Configure the panda (seq table triggering).
        # create a group that is waited for before pmac_trigger_logic kicked off on
        # its own with wait=true.
        yield from bps.kickoff(pandabrick)
        yield from bps.kickoff(panda_trigger_logic)
        yield from bps.kickoff(spectroscopy_detector, wait=True)

        # Start the trajectory.
        yield from bps.kickoff(pmac_trigger_logic, wait=True)

        # Wait for the scan to complete whilst continuously collecting the data.
        yield from bps.collect_while_completing(
            flyers=(
                pmac_trigger_logic,
                panda_trigger_logic,
                pandabrick,
                spectroscopy_detector,
            ),
            dets=(
                pandabrick,
                spectroscopy_detector,
            ),
            stream_name="primary",
            flush_period=0.5,
        )

    yield from inner_plan()


def demo_spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
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

    velomax = grid_size / (xsteps * exposure_time)
    if velomax <= 10 * 0.98:
        fly = True
        LOGGER.info("test-rig-bluesky")
        LOGGER.info(
            f"Estimated velocity is {velomax:.2f} mm/sec, performing a fly scan."
        )
    else:
        fly = False
        LOGGER.info(
            f"Estimated velocity is {velomax:.2f} mm/sec, performing a step scan."
        )

    # Move to the start point
    # pmac should handle premove
    yield from bps.mv(
        *(sample_stage.x, xmin), *(sample_stage.y, ymin), group="initial_move"
    )

    grid = Line(sample_stage.y, ymin, ymax, ysteps) * ~Line(  # type: ignore
        sample_stage.x,  # type: ignore
        xmin,
        xmax,
        xsteps,
    )
    # fly = False
    yield from spectroscopy(
        spectroscopy_detector=spectroscopy_detector,
        sample_stage=sample_stage,
        pmac=pmac,
        pandabrick=pandabrick,
        num_points=total_number_of_scan_points,
        spec=grid,
        exposure_time=exposure_time,
        fly=fly,
        metadata=metadata,
    )
