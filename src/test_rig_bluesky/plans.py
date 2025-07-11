from typing import Any

from bluesky.plans import count
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.common.beamlines.beamline_utils import get_path_provider
from dodal.devices.motors import XYZStage
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from dodal.plans import spec_scan
from ophyd_async.epics.adaravis import AravisDetector
from scanspec.specs import Line, Spec

sample_det = inject("sample_det")
oav = inject("oav")
sample_stage = inject("sample_stage")


@attach_data_session_metadata_decorator(get_path_provider())
def snapshot(
    sample_det: AravisDetector = sample_det,
    oav: AravisDetector = oav,
    sample_stage: XYZStage = sample_stage,
) -> MsgGenerator[None]:
    """Capture a snapshot of the current state of the beamline."""
    yield from count([sample_det, oav, sample_stage])


def spectroscopy(
    oav: AravisDetector = oav,
    sample_stage: XYZStage = sample_stage,
    spec: Spec | None = None,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a spectroscopy scan."""
    spec = spec or Line(sample_stage.x, 0, 5, 5)

    yield from spec_scan({oav, sample_stage}, spec, metadata=metadata)
