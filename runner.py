from bluesky import RunEngine

from test_rig_bluesky.plans import spectroscopy_flyscan

RE = RunEngine()
RE(spectroscopy_flyscan())
