import logging
import math
from pathlib import Path
from typing import Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.plans import count
from bluesky.protocols import Movable, Readable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from ophyd_async.core import (
    DetectorTrigger,
    Device,
    Settings,
    SettingsProvider,
    StandardDetector,
    TriggerInfo,
    YamlSettingsProvider,
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
from pydantic import TypeAdapter
from scanspec.specs import Fly, Spec

LOGGER = logging.getLogger(__name__)

# Shared hardware only. Experiment-specific devices are injected in the module
# for that experiment, so that this one can be imported by both.
pmac = inject("pmac")
pandabrick = inject("pandabrick")


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
    baselines_directory = Path(__file__).parent / "baselines"
    return YamlSettingsProvider(baselines_directory)


def serialize_spec(spec: Spec[Any]) -> Any:
    """Render a Spec as something JSON-serializable, for scan metadata."""
    # NOTE: replace with spec.serialize() when this merges:
    # https://github.com/bluesky/scanspec/pull/208
    return TypeAdapter(Spec[Any]).dump_python(spec, mode="json", fallback=repr)


@attach_data_session_metadata_decorator()
def snapshot(devices: list[Readable]) -> MsgGenerator[None]:
    """Capture a snapshot of the current state of the given devices.

    Useful as a smoke test for new hardware: it exercises device connection and
    the data-writing path without any triggering complexity.
    """
    yield from count(devices)


def fly_scan(
    spec: Spec[Movable],
    detector: StandardDetector,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    exposure_time: float = 0.1,
    detector_deadtime: float = 2e-3 * 1.01,
    detector_trigger: DetectorTrigger = DetectorTrigger.EXTERNAL_EDGE,
    panda_whitelist: list[str] | None = None,
    plan_name: str = "fly_scan",
    metadata: dict[str, Any] | None = None,
):
    yield from load_panda_settings(
        panda=pandabrick,
        design_name="pandabrick_baseline",
        whitelist_pvs=panda_whitelist,
    )

    scan_frame_duration = exposure_time
    fly_spec = Fly(scan_frame_duration @ spec)  # type: ignore

    # Derived from the spec rather than passed in: if the two disagree the scan
    # either truncates or hangs waiting for frames that never arrive.
    num_points = math.prod(fly_spec.shape())

    table: SeqBlock = pandabrick.seq[1]  # type: ignore # noqa: SLF001

    pmac_scan_info = PmacScanInfo(spec=fly_spec, ramp_time=None, turnaround_time=None)  # type: ignore
    scan_spec_info = ScanSpecInfo(spec=fly_spec, deadtime=detector_deadtime)  # type: ignore

    # The trajectory and seq-table logics are bare FlyableLogic; wrap each in an
    # ephemeral StandardFlyable so the RunEngine can prepare/kickoff/complete it.
    pmac_trigger_logic = PmacTrajectoryFlyableLogic(pmac).with_device(
        name="pmac_trigger_logic"
    )
    panda_trigger_logic = ScanSpecSeqTableFlyableLogic(table).with_device(
        name="panda_trigger_logic"
    )

    scan_frame_livetime = scan_frame_duration - detector_deadtime

    # Reproduce metadata present in spec_scan
    _md = {
        "plan_args": {
            "detectors": {det.name for det in [pandabrick, detector]},
            "spec": repr(fly_spec),
        },
        "plan_name": plan_name,
        "shape": fly_spec.shape(),
        **(metadata or {}),
    }

    # Prepare Panda file writer trigger info.
    # The PandA HDF writer takes EXTERNAL_LEVEL and the camera EXTERNAL_EDGE;
    # the difference is deliberate. Getting these wrong causes silent
    # frame-count mismatches.
    panda_hdf_info = TriggerInfo(
        number_of_events=num_points,
        trigger=DetectorTrigger.EXTERNAL_LEVEL,
        livetime=scan_frame_livetime,
        deadtime=detector_deadtime,
    )

    detector_info = TriggerInfo(
        number_of_events=num_points,
        trigger=detector_trigger,
        livetime=scan_frame_livetime,
        deadtime=detector_deadtime,
    )

    @attach_data_session_metadata_decorator()
    @bpp.run_decorator(md=_md)
    @bpp.stage_decorator(
        [pandabrick, panda_trigger_logic, detector, pmac_trigger_logic]
    )
    def inner_plan():
        # Prepare pmac with the trajectory
        yield from bps.prepare(pmac_trigger_logic, pmac_scan_info)
        # prepare sequencer table
        yield from bps.prepare(panda_trigger_logic, scan_spec_info)
        # prepare panda and hdf writer once, at start of scan
        yield from bps.prepare(pandabrick, panda_hdf_info)
        # prepare detector and info
        # waiting for this last prepare means all prepare functions will be complete
        yield from bps.prepare(detector, detector_info, wait=True)

        # Need to run this after detectors are prepared
        yield from bps.declare_stream(
            pandabrick, detector, name="primary", collect=True
        )

        # Start the detectors and hdf writers acquiring.
        # Configure the panda (seq table triggering).
        # create a group that is waited for before pmac_trigger_logic kicked off on
        # its own with wait=true.
        yield from bps.kickoff(pandabrick)
        yield from bps.kickoff(panda_trigger_logic)
        yield from bps.kickoff(detector, wait=True)

        # Start the trajectory.
        yield from bps.kickoff(pmac_trigger_logic, wait=True)

        # Wait for the scan to complete whilst continuously collecting the data.
        yield from bps.collect_while_completing(
            flyers=(
                pmac_trigger_logic,
                panda_trigger_logic,
                pandabrick,
                detector,
            ),
            dets=(
                pandabrick,
                detector,
            ),
            stream_name="primary",
            flush_period=0.5,
        )

    yield from inner_plan()
