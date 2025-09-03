import math
import os
from typing import Any

from bluesky import plan_stubs as bps
from bluesky.plans import count
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.devices.motors import XYZStage
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from dodal.plans import spec_scan
from ophyd_async.core import YamlSettingsProvider
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.plan_stubs import (
    apply_settings,
    apply_settings_if_different,
    retrieve_settings,
    store_settings,
)
from scanspec.specs import Line, Spec

imaging_detector = inject("imaging_detector")
spectroscopy_detector = inject("spectroscopy_detector")
sample_stage = inject("sample_stage")

yaml_directory = os.path.abspath("./src/test_rig_bluesky/")
yaml_filename = "spectroscopy_detector_baseline"


def save_settings(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    yaml_directory: str = yaml_directory,
    yaml_filename: str = yaml_filename,
):
    provider = YamlSettingsProvider(yaml_directory)
    yield from store_settings(provider, yaml_filename, spectroscopy_detector)


def load_settings(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    yaml_directory: str = yaml_directory,
    yaml_filename: str = yaml_filename,
):
    provider = YamlSettingsProvider(yaml_directory)
    settings = yield from retrieve_settings(
        provider, yaml_filename, spectroscopy_detector
    )
    yield from apply_settings_if_different(settings, apply_settings)


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
    spec: Spec[Movable] | None = None,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a spectroscopy scan."""
    yield from load_settings(spectroscopy_detector, yaml_directory, yaml_filename)

    for motor in [sample_stage.x, sample_stage.y]:
        yield from bps.mv(
            *(motor.acceleration_time, 0.001),
            *(motor.velocity, 100),
            wait=True,
        )

    spec = spec or Line(sample_stage.x, 0, 5, 5)

    yield from spec_scan({spectroscopy_detector, sample_stage}, spec, metadata=metadata)


def demo_spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    total_number_of_scan_points: int = 25,
    grid_size: float = 5.0,
    grid_origin_x: float = 0.0,
    grid_origin_y: float = 0.0,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Spectroscopy plan intended for use in Visr demonstrations to visitors.
    The time taken to scan is approximately linear in total_numbers_of_grid_points.
    All other parameters can be left at their defaults.
    """
    xsteps = ysteps = int(round(math.sqrt(max(total_number_of_scan_points, 1))))
    xmin = grid_origin_x
    xmax = grid_origin_x + grid_size
    ymin = grid_origin_y
    ymax = grid_origin_y + grid_size
    grid = Line(sample_stage.y, ymin, ymax, ysteps) * Line(
        sample_stage.x, xmin, xmax, xsteps
    )
    yield from spectroscopy(
        spectroscopy_detector=spectroscopy_detector,
        sample_stage=sample_stage,
        spec=grid,
        exposure_time=exposure_time,
        metadata=metadata,
    )
