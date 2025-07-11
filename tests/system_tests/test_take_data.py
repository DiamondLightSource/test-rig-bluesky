def test_collect_data(bluesky_plan_runner) -> None:
    bluesky_plan_runner.run("count", {"detectors": ["sample_det"], "num": 5}, 10.0)
