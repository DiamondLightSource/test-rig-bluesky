"""Bluesky plans for tomography experiments.

Shares the motion controller (PMAC) and the triggering / position-capture
hardware (PandA) with the spectroscopy plans. Everything axis- and
experiment-agnostic lives in ``plans.py``; this module holds only the
rotation axis, the tomography detector, and their configuration.
"""

import logging
from enum import StrEnum
from typing import Any

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
from scanspec.specs import Line, Spec

from .plans import fly_scan, load_settings, pandabrick, pmac, serialize_spec

LOGGER = logging.getLogger(__name__)

# pmac and pandabrick come from plans.py -- same controller, same brick as
# spectroscopy. Only the detector and the rotation axis are ours.
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
# calibration images taken under the same illumination. The filename cannot
# carry this: it comes from the path provider, which is built once per device
# and is not a plan's to change.
CALIBRATION_TYPE_KEY = "calibration_type"
LIGHT_SOURCE_KEY = "light_source"


# Hardware facts, so a blueapi user cannot get them wrong. exposure_time stays
# a plan argument because it is genuinely per-scan.

# The tomography camera is an Alvium 1800 U-052, a different model from the
# spectroscopy Manta, so these are deliberately NOT shared with
# spectroscopy_plans - deadtime and readout pad are per camera model.
#
# TODO: both values below are placeholders carried over from the Manta and are
# almost certainly too large for the Alvium. ophyd-async's table in
# epics/adgenicam.py has no Alvium entry, and nothing cross-checks these
# because AreaDetector.prepare uses the TriggerInfo we build rather than
# calling get_deadtime(). Get the real figures from the Allied Vision
# datasheet, or measure them.
#
# The error is in the safe direction - too long a period costs frame rate
# rather than dropping frames - but it caps the minimum usable exposure at
# ~2 ms, because livetime = exposure_time - deadtime must stay non-negative or
# TriggerInfo rejects it.
ALVIUM_ACQUIRE_PERIOD_PAD = 1961e-6
ALVIUM_DETECTOR_DEADTIME = 2e-3 * 1.01

TOMOGRAPHY_DETECTOR_TRIGGER = DetectorTrigger.EXTERNAL_EDGE

# No roistat entries: tomography wants raw projections, not ROI sums, so the
# NDAttribute plumbing from the spectroscopy plan is deliberately absent.
DETECTOR_WHITELIST = [
    "hdf-nd_array_port",
    "hdf-enable_callbacks",
    "driver-acquire",
    "driver-trigger_mode",
    "driver-trigger_source",
]

# A bare Motor has no axis prefix, unlike XYZStage's "x-velocity".
TOMOGRAPHY_STAGE_WHITELIST = [
    "acceleration_time",
    "velocity",
]

# TODO: confirm which PandA encoder input the rotation axis is wired to.
# Spectroscopy uses 2 and 3 for x and y, so this is likely 1 or 4. This is a
# wiring fact, not something to guess at.
PANDA_WHITELIST = ["incenc-1-val_dataset"]


def _setup_detector(
    detector: AravisDetector,
    exposure_time: float,
) -> MsgGenerator[None]:
    """Put the detector in its scanning state.

    Shared by the projections and the calibration images deliberately: flats
    have to be taken at the same exposure, gain, binning and ROI as the
    projections or the normalisation introduces artefacts that look like real
    features. Routing both through here is what stops the two drifting apart.
    """
    yield from load_settings(
        device=detector,
        design_name="tomography_detector_baseline",
        whitelist_pvs=DETECTOR_WHITELIST,
    )

    # mv rather than prepare, because prepare cannot be used outside a run.
    # See: https://github.com/DiamondLightSource/blueapi/issues/1211
    #
    # NOTE: on the fly path acquire_time is overwritten during prepare -
    # AravisTriggerLogic.prepare_edge sets it to the TriggerInfo livetime,
    # i.e. exposure_time minus the deadtime. This mv is what takes effect for
    # internally triggered collection, and it sets acquire_period either way.
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

    :param light_source: required for flats, ignored for darks. A dark field is
        taken with the beam off, so the illumination it will be used with is not
        a property of the measurement - one set of darks serves both. Pass the
        same value you will pass to ``tomography``.

    This plan does not operate the shutter, the LED or the sample stage - set
    the beam and sample up for the kind of image you are taking before running
    it. It only guarantees the detector is configured identically to the
    projections, which is the part that is easy to get silently wrong.

    Images are internally triggered: the PandA is not involved, and
    ophyd-async turns external triggering off when preparing for a count.
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
    num_projections: int = 360,
    angular_range: float = 360.0,
    start_angle: float = 0.0,
    exposure_time: float = 0.1,
    fly: bool = True,
    spec: Spec[Movable] | None = None,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a tomography scan over the rotation axis.

    Time taken is approximately linear in num_projections. Pass fly=False to
    step through the projections instead of flying them; nothing here inspects
    the velocity, so an infeasible fly scan is rejected by the PMAC at prepare
    with a message naming the motor and its limit.

    :param light_source: recorded in metadata so the reconstruction workflow
        knows which calibration images to pair with these projections. It has
        no default on purpose - guessing it wrong mislabels the data silently.

    :param spec: expert override. If given, the geometry parameters are ignored
        and the stage is neither moved to the start nor wound back afterwards,
        since the caller owns where the scan begins and ends.

    Flat and dark fields are taken separately and live on the filesystem; the
    reconstruction workflow pairs them with these projections, so this plan
    does not record them.
    """
    yield from _setup_detector(tomography_detector, exposure_time)

    yield from load_settings(
        device=tomography_stage,
        design_name="tomography_stage_baseline",
        whitelist_pvs=TOMOGRAPHY_STAGE_WHITELIST,
    )

    # A single Line -- no product, no snake. Single-axis continuous rotation is
    # the easiest case for the trajectory: no turnarounds mid-scan.
    #
    # scanspec's Line is inclusive of both endpoints, so the last projection is
    # placed one step short of start + angular_range. Over a full turn that
    # keeps the step a clean angular_range / num_projections and stops the
    # first and last projections being the same view.
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
    # here is reported before the trajectory is built. This happens after the
    # stage baseline is loaded so the move uses the baseline velocity.
    yield from bps.mv(tomography_stage, start_angle, group="initial_move")

    def wind_back() -> MsgGenerator[None]:
        # The scan finishes near last_angle, plus whatever run-down the PMAC
        # adds, so unwind to leave the stage where the next scan expects it.
        # This is a full reverse rotation, not a short hop to the equivalent
        # angle: motor positions are linear, not modulo 360. Winding back also
        # stops repeated scans accumulating cable wrap.
        yield from bps.mv(tomography_stage, start_angle, group="wind_back")

    yield from bpp.finalize_wrapper(scan(), wind_back)
