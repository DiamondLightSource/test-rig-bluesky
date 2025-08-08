from typing import Any

from bluesky import plan_stubs as bps
from bluesky.plans import count
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.devices.motors import XYZStage
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from dodal.plans import spec_scan
from ophyd_async.core import TriggerInfo
from ophyd_async.epics.adaravis import AravisDetector
from scanspec.specs import Line, Spec

imaging_detector = inject("imaging_detector")
spectroscopy_detector = inject("spectroscopy_detector")
sample_stage = inject("sample_stage")


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
    spec: Spec | None = None,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a spectroscopy scan."""
    yield from bps.prepare(
        spectroscopy_detector, TriggerInfo(livetime=exposure_time), wait=True
    )

    spec = spec or Line(sample_stage.x, 0, 5, 5)

    yield from spec_scan({spectroscopy_detector, sample_stage}, spec, metadata=metadata)
