"""Bluesky plans for tomography experiments."""

import logging
from enum import StrEnum
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
    """Deprecated: only kept so the demo_tomography API doesn't change. Ignored."""

    LED = "led"
    SR = "sr"
    DARK = "dark"


class ImageType(StrEnum):
    """What a run of images is, so the reconstruction workflow can tell them apart."""

    DARK = "dark"
    FLAT = "flat"
    PROJECTION = "projection"


# Metadata key holding the run's ImageType
IMAGE_KEY = "image_key"
# Shared by the darks, flats and projections runs of one tomography scan
GROUP_KEY = "tomography_group"
# Recorded on the projections run, pointing at its calibration runs
DARKS_UID_KEY = "darks_uid"
FLATS_UID_KEY = "flats_uid"

# Tomography stage x positions, in mm
SAMPLE_OUT_X = 0.0  # TODO: placeholder, sample out of the field of view for flats
SAMPLE_IN_X = 0.0  # TODO: placeholder, sample back in the field of view

# Where the sample sits and whether the tomography light is on for each image type.
# The spectroscopy mirror is always flipped out of the way.
SAMPLE_X_AND_LIGHT: dict[ImageType, tuple[float, bool]] = {
    ImageType.DARK: (SAMPLE_OUT_X, False),
    ImageType.FLAT: (SAMPLE_OUT_X, True),
    ImageType.PROJECTION: (SAMPLE_IN_X, True),
}

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
ALVIUM_ACQUIRE_PERIOD_PAD = 25e-3
ALVIUM_DETECTOR_DEADTIME = 25e-3

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


def _setup_detector(detector: AravisDetector, exposure_time: float) -> MsgGenerator:
    """Put the detector in its scanning state."""
    # ---- load baseline settings ----------------------------------------------
    yield from load_settings(
        device=detector,
        design_name="tomography_detector_baseline",
        whitelist_pvs=DETECTOR_WHITELIST,
    )

    # ---- set exposure --------------------------------------------------------
    # mv rather than prepare, because prepare cannot be used outside a run.
    # See: https://github.com/DiamondLightSource/blueapi/issues/1211
    yield from bps.mv(
        *(detector.driver.acquire_time, exposure_time),
        *(
            detector.driver.acquire_period,
            exposure_time + ALVIUM_ACQUIRE_PERIOD_PAD,
        ),
        group="detector_acquire",
    )


def _move_for(
    image_type: ImageType,
    stage: XThetaStage,
    tomography_flip_mirror: FlipMirror,
    spectroscopy_flip_mirror: FlipMirror,
) -> MsgGenerator:
    """Move the sample and mirrors into place for this type of image."""
    sample_x, light_on = SAMPLE_X_AND_LIGHT[image_type]
    yield from bps.mv(
        *(stage.x, sample_x),
        *(spectroscopy_flip_mirror, False),
        *(tomography_flip_mirror, light_on),
        group=f"move_for_{image_type.value}",
    )


def _collect_calibration(
    image_type: ImageType,
    num_images: int,
    detector: AravisDetector,
    stage: XThetaStage,
    tomography_flip_mirror: FlipMirror,
    spectroscopy_flip_mirror: FlipMirror,
    metadata: dict[str, Any],
) -> MsgGenerator[str]:
    """Collect dark or flat images, returning the run's uid."""
    LOGGER.info("Collecting %d %s images.", num_images, image_type.value)
    yield from _move_for(
        image_type, stage, tomography_flip_mirror, spectroscopy_flip_mirror
    )
    return (
        yield from count(
            [detector], num=num_images, md={IMAGE_KEY: image_type.value, **metadata}
        )
    )


def _collect_projections(
    spec: Spec[Movable],
    detector: AravisDetector,
    stage: XThetaStage,
    tomography_flip_mirror: FlipMirror,
    spectroscopy_flip_mirror: FlipMirror,
    pmac: PmacIO,
    pandabrick: HDFPanda,
    exposure_time: float,
    metadata: dict[str, Any],
    scan_type: ScanType,
) -> MsgGenerator:
    """Collect projections over spec, by fly or step scan."""
    # ---- move sample in, light on --------------------------------------------
    yield from _move_for(
        ImageType.PROJECTION, stage, tomography_flip_mirror, spectroscopy_flip_mirror
    )

    # ---- build metadata ------------------------------------------------------
    metadata = {
        IMAGE_KEY: ImageType.PROJECTION.value,
        "spec": serialize_spec(spec),
        **metadata,
    }

    # ---- scan ----------------------------------------------------------------
    if scan_type == ScanType.FLY:
        LOGGER.info("Performing a tomography fly scan.")
        yield from fly_scan(
            spec,
            detector,
            pmac=pmac,
            pandabrick=pandabrick,
            exposure_time=exposure_time,
            detector_deadtime=ALVIUM_DETECTOR_DEADTIME,
            detector_trigger=DETECTOR_TRIGGER,
            pandabrick_whitelist=PANDA_WHITELIST,
            plan_name="tomography_fly_scan",
            metadata=metadata,
        )
    elif scan_type == ScanType.STEP:
        LOGGER.info("Performing a tomography step scan.")
        yield from spec_scan(
            {detector, stage},
            spec,  # type: ignore
            metadata=metadata,
        )


def _collect_images(
    spec: Spec[Movable],
    detector: AravisDetector,
    stage: XThetaStage,
    tomography_flip_mirror: FlipMirror,
    spectroscopy_flip_mirror: FlipMirror,
    pmac: PmacIO,
    pandabrick: HDFPanda,
    exposure_time: float,
    num_darks: int,
    num_flats: int,
    metadata: dict[str, Any],
    scan_type: ScanType,
) -> MsgGenerator:
    """Darks, then flats, then projections linked to both."""
    mirrors = (tomography_flip_mirror, spectroscopy_flip_mirror)

    # ---- collect darks -------------------------------------------------------
    darks_uid = yield from _collect_calibration(
        ImageType.DARK, num_darks, detector, stage, *mirrors, metadata
    )

    # ---- collect flats -------------------------------------------------------
    flats_uid = yield from _collect_calibration(
        ImageType.FLAT, num_flats, detector, stage, *mirrors, metadata
    )

    # ---- collect projections -------------------------------------------------
    yield from _collect_projections(
        spec,
        detector,
        stage,
        *mirrors,
        pmac,
        pandabrick,
        exposure_time,
        {DARKS_UID_KEY: darks_uid, FLATS_UID_KEY: flats_uid, **metadata},
        scan_type,
    )


def _restoring_mirrors(plan: MsgGenerator, *mirrors: FlipMirror) -> MsgGenerator:
    """Run plan, then put the mirrors back where they were, even if it fails."""
    initial_positions = []
    for mirror in mirrors:
        initial_positions += [mirror, (yield from bps.rd(mirror.position))]
    return (yield from bpp.finalize_wrapper(plan, bps.mv(*initial_positions)))


def _wind_back(stage: XThetaStage, start_angle: float) -> MsgGenerator[None]:
    # unwind to leave the stage where the next scan expects it.
    # currently this winds back the full rotation but should look
    # into changing it so that we just advance one step to the
    # beginning again and reset the angle to 0?
    yield from bps.mv(stage.theta, start_angle, group="wind_back")


def tomography(
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
) -> MsgGenerator:
    """Do a tomography scan: darks, then flats with the sample out, then projections."""
    # ---- set up detector and stage -------------------------------------------
    yield from _setup_detector(detector, exposure_time)
    yield from load_settings(
        device=stage,
        design_name="tomography_stage_baseline",
        whitelist_pvs=STAGE_WHITELIST,
    )

    # ---- define scan ---------------------------------------------------------
    # One projection per degree over a full rotation
    spec = spec or Line(stage.theta, 0, 359, 360)  # type: ignore
    shared_metadata = {GROUP_KEY: str(uuid4()), **(metadata or {})}

    # ---- collect darks, flats and projections, then restore mirrors ----------
    yield from _restoring_mirrors(
        _collect_images(
            spec,
            detector,
            stage,
            tomography_flip_mirror,
            spectroscopy_flip_mirror,
            pmac,
            pandabrick,
            exposure_time,
            num_darks,
            num_flats,
            shared_metadata,
            scan_type,
        ),
        tomography_flip_mirror,
        spectroscopy_flip_mirror,
    )


def demo_tomography(
    light_source: LightSource,  # deprecated
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
    angular_range from start_angle, then winds the stage back to start_angle."""
    del light_source

    # ---- define projections --------------------------------------------------
    angular_step = angular_range / number_of_projections
    last_angle = start_angle + angular_range - angular_step
    spec = Line(stage.theta, start_angle, last_angle, number_of_projections)  # type: ignore

    # ---- move to start -------------------------------------------------------
    # PMAC handles the run-up, but move to the nominal start first so a failure
    # here is reported before the trajectory is built.
    yield from bps.mv(stage.theta, start_angle, group="initial_move")

    # ---- run tomography, then wind back --------------------------------------
    yield from bpp.finalize_wrapper(
        tomography(
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
