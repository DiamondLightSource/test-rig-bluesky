from bluesky import RunEngine
from dodal.beamlines.b01_1 import oav, sample_det, sample_stage

from test_rig_bluesky.plans import snapshot


def test_snapshot():
    RE = RunEngine()

    _sample_det = sample_det(connect_immediately=True)
    _oav = oav(connect_immediately=True)
    _sample_stage = sample_stage(connect_immediately=True)

    RE(snapshot(_sample_det, _oav, _sample_stage))
