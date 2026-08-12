"""Bluesky plans for tomography experiments.

Shares the motion controller (PMAC) and the triggering / position-capture
hardware (PandA) with the spectroscopy plans. Everything axis- and
experiment-agnostic lives in ``plans.py``; this module holds only the
rotation axis, the tomography detector, and their configuration.
"""

import logging
from typing import Any

import bluesky.plan_stubs as bps
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.plans import spec_scan
from ophyd_async.core import DetectorTrigger
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.motor import Motor
from ophyd_async.epics.pmac import PmacIO
from ophyd_async.fastcs.panda import HDFPanda
from scanspec.specs import Line, Spec

from .plans import (
    ARAVIS_ACQUIRE_PERIOD_PAD,
    fly_scan,
    load_settings,
    pandabrick,
    pmac,
    serialize_spec,
)

LOGGER = logging.getLogger(__name__)

# pmac and pandabrick come from plans.py -- same controller, same brick as
# spectroscopy. Only the detector and the rotation axis are ours.
tomography_detector = inject("tomography_detector")
tomography_stage = inject("tomography_stage")


# Hardware facts, so a blueapi user cannot get them wrong. exposure_time stays
# a plan argument because it is genuinely per-scan.

# The tomography camera is an Aravis, as the spectroscopy one is, so it shares
# the deadtime; the readout pad comes from plans.py for the same reason.
# https://github.com/bluesky/ophyd-async/blob/15fa34b6ea2a28e2f27265a5564c9ee36423f1b7/src/ophyd_async/epics/adaravis/_aravis_controller.py#L11
TOMOGRAPHY_DETECTOR_DEADTIME = 2e-3 * 1.01
TOMOGRAPHY_DETECTOR_TRIGGER = DetectorTrigger.EXTERNAL_EDGE

# Maximum safe velocity of the rotation stage, in deg/s.
MAX_ROTATION_VELOCITY = 90.0

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


def tomography(
    tomography_detector: AravisDetector = tomography_detector,
    tomography_stage: Motor = tomography_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    spec: Spec[Movable] | None = None,
    exposure_time: float = 0.1,
    fly: bool = True,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a tomography scan over the rotation axis.

    Flat and dark fields are taken separately and live on the filesystem; the
    reconstruction workflow pairs them with these projections, so this plan
    does not record them.
    """
    yield from load_settings(
        device=tomography_detector,
        design_name="tomography_detector_baseline",
        whitelist_pvs=DETECTOR_WHITELIST,
    )

    # mv rather than prepare, because prepare cannot be used outside a run.
    # See: https://github.com/DiamondLightSource/blueapi/issues/1211
    yield from bps.mv(
        *(tomography_detector.driver.acquire_time, exposure_time),
        *(
            tomography_detector.driver.acquire_period,
            exposure_time + ARAVIS_ACQUIRE_PERIOD_PAD,
        ),
        group="tomography_detector_acquire",
    )

    yield from load_settings(
        device=tomography_stage,
        design_name="tomography_stage_baseline",
        whitelist_pvs=TOMOGRAPHY_STAGE_WHITELIST,
    )

    # Half-open: 360 projections at 1 degree steps over [0, 360). See
    # demo_tomography for why the endpoint is excluded.
    spec = spec or Line(tomography_stage, 0, 359, 360)  # type: ignore

    if metadata is None:
        metadata = {}
    metadata["spec"] = serialize_spec(spec)

    if fly:
        LOGGER.info("Performing a tomography fly scan.")
        yield from fly_scan(
            spec,
            tomography_detector,
            pmac=pmac,
            pandabrick=pandabrick,
            exposure_time=exposure_time,
            detector_deadtime=TOMOGRAPHY_DETECTOR_DEADTIME,
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


def demo_tomography(
    tomography_detector: AravisDetector = tomography_detector,
    tomography_stage: Motor = tomography_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    num_projections: int = 360,
    angular_range: float = 360.0,
    start_angle: float = 0.0,
    exposure_time: float = 0.1,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Tomography plan with sensible defaults, for demonstration use.

    Time taken is approximately linear in num_projections. All other
    parameters can be left at their defaults.
    """
    angular_step = angular_range / num_projections

    velocity = angular_range / (num_projections * exposure_time)
    if velocity <= MAX_ROTATION_VELOCITY * 0.98:
        fly = True
        LOGGER.info(
            f"Estimated velocity is {velocity:.2f} deg/sec, performing a fly scan."
        )
    else:
        fly = False
        LOGGER.info(
            f"Estimated velocity is {velocity:.2f} deg/sec, performing a step scan."
        )

    # PMAC handles the run-up, but move to the nominal start first so a
    # failure here is reported before the trajectory is built.
    yield from bps.mv(tomography_stage, start_angle, group="initial_move")

    # A single Line -- no product, no snake. Single-axis continuous rotation is
    # the easiest case for the trajectory: no turnarounds mid-scan.
    #
    # scanspec's Line is inclusive of both endpoints, so the last projection is
    # placed one step short of start + angular_range. Over a full turn that
    # keeps the step a clean angular_range / num_projections and stops the
    # first and last projections being the same view.
    last_angle = start_angle + angular_range - angular_step
    scan = Line(tomography_stage, start_angle, last_angle, num_projections)  # type: ignore

    yield from tomography(
        tomography_detector=tomography_detector,
        tomography_stage=tomography_stage,
        pmac=pmac,
        pandabrick=pandabrick,
        spec=scan,
        exposure_time=exposure_time,
        fly=fly,
        metadata=metadata,
    )
