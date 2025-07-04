from bluesky.plans import count
from dodal.beamlines.b01_1 import mako as _mako
from dodal.beamlines.b01_1 import manta as _manta
from dodal.common.beamlines.beamline_utils import get_path_provider
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator


@attach_data_session_metadata_decorator(get_path_provider())
def snapshot():
    """Capture one image on mako + manta and write file to commissioning directory."""
    manta = _manta(connect_immediately=True)
    mako = _mako(connect_immediately=True)
    yield from count([manta, mako])
