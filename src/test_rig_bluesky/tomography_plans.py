"""Bluesky plans for tomography experiments."""

import logging
from enum import StrEnum
from typing import Annotated, Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.plans import count
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.plan_stubs.data_session import attach_data_session_metadata_decorator
from dodal.plans import spec_scan
from ophyd_async.core import DetectorTrigger
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.motor import Motor
from ophyd_async.epics.pmac import PmacIO
from ophyd_async.fastcs.panda import HDFPanda
from pydantic import Field
from scanspec.specs import Line, Spec

from .plans import fly_scan, load_settings, pandabrick, pmac, serialize_spec

LOGGER = logging.getLogger(__name__)

# pmac and pandabrick come from plans.py
tomography_detector = inject("tomography_detector")
tomography_stage = inject("tomography_stage")


class CalibrationType(StrEnum):
    """Which kind of calibration image is being collected."""

    FLAT = "flat"
    """Beam on, sample out: the illumination profile to normalise against."""
    DARK = "dark"
    """Beam off: the detector's own offset and noise floor."""


class LightSource(StrEnum):
    """The illumination in use, so calibration images can be matched to scans."""

    LED = "led"
    SR = "sr"


# Metadata keys the reconstruction workflow reads to pair projections with the
# calibration images taken under the same illumination
CALIBRATION_TYPE_KEY = "calibration_type"
LIGHT_SOURCE_KEY = "light_source"

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


# TODO: both values below are placeholders carried over from the Manta and are
# almost certainly too large for the Alvium. Get real figures from datasheet
ALVIUM_ACQUIRE_PERIOD_PAD = 1961e-6
ALVIUM_DETECTOR_DEADTIME = 2e-3 * 1.01

TOMOGRAPHY_DETECTOR_TRIGGER = DetectorTrigger.EXTERNAL_EDGE

DETECTOR_WHITELIST = [
    "hdf-nd_array_port",
    "hdf-enable_callbacks",
    "driver-acquire",
    "driver-trigger_mode",
    "driver-trigger_source",
]

TOMOGRAPHY_STAGE_WHITELIST = [
    "acceleration_time",
    "velocity",
]

# TODO: confirm which PandA encoder input the rotation axis is wired to.
PANDA_WHITELIST = ["incenc-8-val_dataset"]


def _setup_detector(
    detector: AravisDetector,
    exposure_time: float,
) -> MsgGenerator[None]:
    """Put the detector in its scanning state."""
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
            exposure_time + ALVIUM_ACQUIRE_PERIOD_PAD,
        ),
        group="tomography_detector_acquire",
    )


@attach_data_session_metadata_decorator()
def collect_calibration_images(
    calibration_type: CalibrationType,
    light_source: LightSource | None = None,
    tomography_detector: AravisDetector = tomography_detector,
    num_images: int = 20,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Collect flat or dark field images for the reconstruction to normalise with.

    Recorded in the run's metadata under "calibration_type" and, for flats,
    "light_source", so the workflow can find the right images for a given scan.

    :param light_source: required for flats, ignored for darks.
    """
    run_metadata: dict[str, Any] = {CALIBRATION_TYPE_KEY: calibration_type.value}
    if calibration_type is CalibrationType.FLAT:
        if light_source is None:
            raise ValueError(
                "light_source is required for flat fields, so the workflow can "
                "match them to scans taken under the same illumination."
            )
        run_metadata[LIGHT_SOURCE_KEY] = light_source.value
        LOGGER.info(
            "Collecting %d flat images under %s illumination.",
            num_images,
            light_source.value,
        )
    else:
        if light_source is not None:
            LOGGER.info(
                "Ignoring light_source=%s: dark fields are taken with the beam "
                "off, so one set serves every illumination.",
                light_source.value,
            )
        LOGGER.info("Collecting %d dark images.", num_images)

    run_metadata.update(metadata or {})

    yield from _setup_detector(tomography_detector, exposure_time)

    yield from count([tomography_detector], num=num_images, md=run_metadata)


def tomography(
    light_source: LightSource,
    tomography_detector: AravisDetector = tomography_detector,
    tomography_stage: Motor = tomography_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    num_projections: NumProjections = 360,
    angular_range: float = 360.0,
    start_angle: float = 0.0,
    exposure_time: float = 0.1,
    fly: bool = True,
    spec: Spec[Movable] | None = None,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a tomography scan."""
    yield from _setup_detector(tomography_detector, exposure_time)

    yield from load_settings(
        device=tomography_stage,
        design_name="tomography_stage_baseline",
        whitelist_pvs=TOMOGRAPHY_STAGE_WHITELIST,
    )

    caller_supplied_spec = spec is not None
    if spec is None:
        angular_step = angular_range / num_projections
        last_angle = start_angle + angular_range - angular_step
        spec = Line(tomography_stage, start_angle, last_angle, num_projections)  # type: ignore

    if metadata is None:
        metadata = {}
    metadata["spec"] = serialize_spec(spec)
    # Tells the workflow which calibration images belong with these projections.
    metadata[LIGHT_SOURCE_KEY] = light_source.value

    def scan() -> MsgGenerator[None]:
        if fly:
            LOGGER.info("Performing a tomography fly scan.")
            yield from fly_scan(
                spec,
                tomography_detector,
                pmac=pmac,
                pandabrick=pandabrick,
                exposure_time=exposure_time,
                detector_deadtime=ALVIUM_DETECTOR_DEADTIME,
                detector_trigger=TOMOGRAPHY_DETECTOR_TRIGGER,
                panda_whitelist=PANDA_WHITELIST,
                plan_name="tomography_fly_scan",
                metadata=metadata,
            )
        else:
            LOGGER.info("Performing a tomography step scan.")
            yield from spec_scan(
                {tomography_detector, tomography_stage},
                spec,  # type: ignore
                metadata=metadata,
            )

    if caller_supplied_spec:
        # The caller owns the geometry, so we do not know where it starts or
        # what it would mean to unwind to.
        yield from scan()
        return

    # PMAC handles the run-up, but move to the nominal start first so a failure
    # here is reported before the trajectory is built.
    yield from bps.mv(tomography_stage, start_angle, group="initial_move")

    def wind_back() -> MsgGenerator[None]:
        # unwind to leave the stage where the next scan expects it.
        # currently this winds back the full rotation but should look
        # into changing it so that we just advance one step to the
        # beginning again and reset the angle to 0?
        yield from bps.mv(tomography_stage, start_angle, group="wind_back")

    yield from bpp.finalize_wrapper(scan(), wind_back)
