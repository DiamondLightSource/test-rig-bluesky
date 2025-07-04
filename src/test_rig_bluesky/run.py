from bluesky.run_engine import RunEngine

from test_rig_bluesky.plans import snapshot

RE = RunEngine()
RE(snapshot())
