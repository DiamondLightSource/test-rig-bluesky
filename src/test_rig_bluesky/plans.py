from bluesky.plans import count
from dodal.common import inject
from dodal.common.beamlines.beamline_utils import get_path_provider
from dodal.devices.motors import XYZStage
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from ophyd_async.epics.adaravis import AravisDetector

sample_det = inject("sample_det")
oav = inject("oav")
sample_stage = inject("sample_stage")


@attach_data_session_metadata_decorator(get_path_provider())
def snapshot(
    sample_det: AravisDetector = sample_det,
    oav: AravisDetector = oav,
    sample_stage: XYZStage = sample_stage,
):
    """Capture a snapshot of the current state of the beamline."""
    yield from count([sample_det, oav, sample_stage])
