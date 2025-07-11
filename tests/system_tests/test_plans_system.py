def test_snapshot(bluesky_plan_runner):
    bluesky_plan_runner.run("snapshot")


def test_spectroscopy(bluesky_plan_runner):
    bluesky_plan_runner.run("spectroscopy")
