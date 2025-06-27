import subprocess
import sys

from test_rig_bluesky import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "test_rig_bluesky", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
