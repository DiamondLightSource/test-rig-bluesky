"""Bluesky plans for tomography experiments."""

import logging
from enum import StrEnum
from itertools import chain
from typing import Annotated, Any
from uuid import uuid4

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.plans import count
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.devices.beamlines.b01_1.flip_mirror import FlipMirror
from dodal.devices.motors import XThetaStage
from dodal.plans import spec_scan
from ophyd_async.core import DetectorTrigger
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.pmac import PmacIO
from ophyd_async.fastcs.panda import HDFPanda
from pydantic import Field
from scanspec.specs import Line, Spec

from .plans import ScanType, fly_scan, load_settings, pandabrick, pmac, serialize_spec

LOGGER = logging.getLogger(__name__)

# pmac and pandabrick come from plans.py
detector = inject("tomography_detector")
stage = inject("tomography_stage")
tomography_flip_mirror = inject("tomography_flip_mirror")
spectroscopy_flip_mirror = inject("spectroscopy_flip_mirror")


class LightSource(StrEnum):
    """The illumination in use."""

    LED = "led"
    SR = "sr"
    DARK = "dark"


# Metadata keys the reconstruction workflow reads to pair projections with the
# calibration images taken under the same illumination
LIGHT_SOURCE_KEY = "light_source"
# "dark", "flat" or "projection"
IMAGE_KEY = "image_key"
# Shared by the darks, flats and projections runs of one tomography scan
GROUP_KEY = "tomography_group"
# Recorded on the projections run, pointing at its calibration runs
DARKS_UID_KEY = "darks_uid"
FLATS_UID_KEY = "flats_uid"

# Tomography stage x positions, in mm
SAMPLE_OUT_X = 0.0  # TODO: placeholder, sample out of the field of view for flats
SAMPLE_IN_X = 0.0  # TODO: placeholder, sample back in the field of view

MIN_PROJECTIONS = 30
MAX_PROJECTIONS = 1440

NumProjections = Annotated[
    int,
    Field(
        ge=MIN_PROJECTIONS,
        le=MAX_PROJECTIONS,
        description=("Number of projections over the angular range"),
    ),
]


# TODO: conservative estimates for now
MAKO_ACQUIRE_PERIOD_PAD = 25e-3
MAKO_DETECTOR_DEADTIME = 25e-3

DETECTOR_TRIGGER = DetectorTrigger.EXTERNAL_EDGE

DETECTOR_WHITELIST = [
    "hdf-nd_array_port",
    "hdf-enable_callbacks",
    "driver-acquire",
    "driver-trigger_mode",
    "driver-trigger_source",
]

STAGE_WHITELIST = [
    "theta-acceleration_time",
    "theta-velocity",
]

# TODO: confirm which PandA encoder input the rotation axis is wired to.
PANDA_WHITELIST = ["incenc-8-val_dataset"]


def _darks_flats_projections(
    detector: AravisDetector,
    stage: XThetaStage,
    tomography_flip_mirror: FlipMirror,
    spectroscopy_flip_mirror: FlipMirror,
    pmac: PmacIO,
    pandabrick: HDFPanda,
    exposure_time: float,
    spec: Spec[Movable],
    num_darks: int,
    num_flats: int,
    metadata: dict[str, Any],
    scan_type: ScanType,
) -> MsgGenerator[None]:
    """Take darks, then flats with the sample out of the way, then projections."""
    LOGGER.info("Collecting %d dark images.", num_darks)
    yield from bps.mv(
        *(stage.x, SAMPLE_OUT_X),
        *(spectroscopy_flip_mirror, False),
        *(tomography_flip_mirror, False),
        group="darks",
    )
    darks_uid = yield from count(
        [detector], num=num_darks, md={IMAGE_KEY: "dark", **metadata}
    )

    LOGGER.info("Collecting %d flat images.", num_flats)
    yield from bps.mv(tomography_flip_mirror, True, group="flats")
    flats_uid = yield from count(
        [detector], num=num_flats, md={IMAGE_KEY: "flat", **metadata}
    )

    yield from bps.mv(stage.x, SAMPLE_IN_X, group="projections")
    projections_metadata = {
        IMAGE_KEY: "projection",
        DARKS_UID_KEY: darks_uid,
        FLATS_UID_KEY: flats_uid,
        "spec": serialize_spec(spec),
        **metadata,
    }

    if scan_type == ScanType.FLY:
        LOGGER.info("Performing a tomography fly scan.")
        yield from fly_scan(
            spec,
            detector,
            pmac=pmac,
            pandabrick=pandabrick,
            exposure_time=exposure_time,
            detector_deadtime=MAKO_DETECTOR_DEADTIME,
            detector_trigger=DETECTOR_TRIGGER,
            pandabrick_whitelist=PANDA_WHITELIST,
            plan_name="tomography_fly_scan",
            metadata=projections_metadata,
        )
    elif scan_type == ScanType.STEP:
        LOGGER.info("Performing a tomography step scan.")
        yield from spec_scan(
            {detector, stage},
            spec,  # type: ignore
            metadata=projections_metadata,
        )


def _restore_mirrors(positions: list[tuple[FlipMirror, bool]]) -> MsgGenerator[None]:
    yield from bps.mv(*chain.from_iterable(positions))


def _wind_back(stage: XThetaStage, start_angle: float) -> MsgGenerator[None]:
    # unwind to leave the stage where the next scan expects it.
    # currently this winds back the full rotation but should look
    # into changing it so that we just advance one step to the
    # beginning again and reset the angle to 0?
    yield from bps.mv(stage.theta, start_angle, group="wind_back")


def tomography(
    light_source: LightSource,
    detector: AravisDetector = detector,
    stage: XThetaStage = stage,
    tomography_flip_mirror: FlipMirror = tomography_flip_mirror,
    spectroscopy_flip_mirror: FlipMirror = spectroscopy_flip_mirror,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    exposure_time: float = 0.1,
    spec: Spec[Movable] | None = None,
    num_darks: int = 20,
    num_flats: int = 20,
    metadata: dict[str, Any] | None = None,
    *,
    scan_type: ScanType,
) -> MsgGenerator[None]:
    """Do a tomography scan, taking darks and flats first.

    The darks, flats and projections are each their own run, sharing a
    "tomography_group" id, and the projections run records the uids of the other
    two. The flip mirrors are put back as they were afterwards.

    :param light_source: illumination the flats and projections are taken under.
    """
    yield from load_settings(
        device=detector,
        design_name="tomography_detector_baseline",
        whitelist_pvs=DETECTOR_WHITELIST,
    )

    # mv rather than prepare, because prepare cannot be used outside a run.
    # See: https://github.com/DiamondLightSource/blueapi/issues/1211
    yield from bps.mv(
        *(detector.driver.acquire_time, exposure_time),
        *(
            detector.driver.acquire_period,
            exposure_time + MAKO_ACQUIRE_PERIOD_PAD,
        ),
        group="detector_acquire",
    )

    yield from load_settings(
        device=stage,
        design_name="tomography_stage_baseline",
        whitelist_pvs=STAGE_WHITELIST,
    )

    # One projection per degree over a full rotation
    spec = spec or Line(stage.theta, 0, 359, 360)  # type: ignore

    # Tells the workflow which calibration images belong with these projections.
    shared_metadata: dict[str, Any] = {
        GROUP_KEY: str(uuid4()),
        LIGHT_SOURCE_KEY: light_source.value,
        **(metadata or {}),
    }

    initial_mirror_positions = [
        (tomography_flip_mirror, (yield from bps.rd(tomography_flip_mirror.position))),
        (
            spectroscopy_flip_mirror,
            (yield from bps.rd(spectroscopy_flip_mirror.position)),
        ),
    ]

    yield from bpp.finalize_wrapper(
        _darks_flats_projections(
            detector,
            stage,
            tomography_flip_mirror,
            spectroscopy_flip_mirror,
            pmac,
            pandabrick,
            exposure_time,
            spec,
            num_darks,
            num_flats,
            shared_metadata,
            scan_type,
        ),
        _restore_mirrors(initial_mirror_positions),
    )


def demo_tomography(
    light_source: LightSource,
    detector: AravisDetector = detector,
    stage: XThetaStage = stage,
    tomography_flip_mirror: FlipMirror = tomography_flip_mirror,
    spectroscopy_flip_mirror: FlipMirror = spectroscopy_flip_mirror,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    number_of_projections: NumProjections = 360,
    angular_range: float = 360.0,
    start_angle: float = 0.0,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
    *,
    scan_type: ScanType,
) -> MsgGenerator[None]:
    """Tomography plan intended for use in Visr demonstrations to visitors.
    Takes darks and flats, then number_of_projections evenly spaced over
    angular_range from start_angle, then winds the stage back to start_angle.
    """
    angular_step = angular_range / number_of_projections
    last_angle = start_angle + angular_range - angular_step
    spec = Line(stage.theta, start_angle, last_angle, number_of_projections)  # type: ignore

    # PMAC handles the run-up, but move to the nominal start first so a failure
    # here is reported before the trajectory is built.
    yield from bps.mv(stage.theta, start_angle, group="initial_move")

    yield from bpp.finalize_wrapper(
        tomography(
            light_source,
            detector=detector,
            stage=stage,
            tomography_flip_mirror=tomography_flip_mirror,
            spectroscopy_flip_mirror=spectroscopy_flip_mirror,
            pmac=pmac,
            pandabrick=pandabrick,
            exposure_time=exposure_time,
            spec=spec,
            metadata=metadata,
            scan_type=scan_type,
        ),
        _wind_back(stage, start_angle),
    )
