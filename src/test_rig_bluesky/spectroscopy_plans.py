"""Bluesky plans for spectroscopy experiments.

Shares the motion controller (PMAC) and the triggering / position-capture
hardware (PandA) with the other experiment modules; everything
experiment-agnostic lives in ``plans.py``.
"""

import logging
import math
from typing import Any

import bluesky.plan_stubs as bps
from bluesky.protocols import Movable
from bluesky.utils import MsgGenerator
from dodal.common import inject
from dodal.devices.motors import XYZStage
from dodal.plans import spec_scan
from ophyd_async.epics.adaravis import AravisDetector
from ophyd_async.epics.adcore import (
    NDAttributeDataType,
    NDAttributeParam,
    NDROIStatNIO,
    setup_ndattributes,
)
from ophyd_async.epics.pmac import PmacIO
from ophyd_async.fastcs.panda import HDFPanda
from scanspec.specs import Line, Spec

from .plans import fly_scan, load_settings, pandabrick, pmac, serialize_spec

LOGGER = logging.getLogger(__name__)

spectroscopy_detector = inject("spectroscopy_detector")
sample_stage = inject("sample_stage")

# The encoder entries map to specific axes, so despite the PandA being shared
# hardware this list is spectroscopy's.
PANDA_WHITELIST = ["incenc-3-val_dataset", "incenc-2-val_dataset"]

MANTA_ACQUIRE_PERIOD_PAD = 1961e-6
MANTA_DETECTOR_DEADTIME = 2e-3 * 1.01

# Maximum safe velocity of the sample stage, in mm/s.
MAX_STAGE_VELOCITY = 10.0


def spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
    spec: Spec[Movable] | None = None,
    exposure_time: float = 0.1,
    fly: bool = True,
    metadata: dict[str, Any] | None = None,
) -> MsgGenerator[None]:
    """Do a spectroscopy scan."""
    yield from load_settings(
        device=spectroscopy_detector,
        design_name="spectroscopy_detector_baseline",
        whitelist_pvs=[
            "hdf-nd_array_port",
            "hdf-enable_callbacks",
            "driver-acquire",
            "driver-trigger_mode",
            "driver-trigger_source",
            "roistat-channels-array_counter",
            "roistat-channels-1-min_x",
            "roistat-channels-1-min_y",
            "roistat-channels-1-name_",
            "roistat-channels-1-size_x",
            "roistat-channels-1-size_y",
            "roistat-channels-1-use",
            "roistat-channels-2-min_x",
            "roistat-channels-2-min_y",
            "roistat-channels-2-name_",
            "roistat-channels-2-size_x",
            "roistat-channels-2-size_y",
            "roistat-channels-2-use",
            "roistat-channels-3-min_x",
            "roistat-channels-3-min_y",
            "roistat-channels-3-name_",
            "roistat-channels-3-size_x",
            "roistat-channels-3-size_y",
            "roistat-channels-3-use",
            "roistat-nd_array_port",
            "roistat-enable_callbacks",
        ],
    )

    # We call mv instead of prepare because prepare cannot technically be used
    # outside of a run.
    # See: https://github.com/DiamondLightSource/blueapi/issues/1211
    #
    # NOTE: on the fly path acquire_time is overwritten during prepare -
    # AravisTriggerLogic.prepare_edge sets it to the TriggerInfo livetime, i.e.
    # exposure_time minus the deadtime. This mv is what takes effect on the
    # step path, and it is what sets acquire_period either way.
    yield from bps.mv(
        *(spectroscopy_detector.driver.acquire_time, exposure_time),
        *(
            spectroscopy_detector.driver.acquire_period,
            exposure_time + MANTA_ACQUIRE_PERIOD_PAD,
        ),
        group="spectroscopy_detector_aquire",
    )

    params: list[NDAttributeParam] = []
    for channel in list(spectroscopy_detector.roistat.channels.keys()):  # type: ignore
        roistatn = spectroscopy_detector.roistat.channels[channel]  # type: ignore
        assert isinstance(roistatn, NDROIStatNIO)

        channel_name = yield from bps.rd(roistatn.name_)

        params.append(
            NDAttributeParam(
                name=f"{channel_name}Total",
                param="ROISTAT_TOTAL",
                datatype=NDAttributeDataType.DOUBLE,
                addr=channel - 1,
                description=f"Sum of {channel_name} channel",
            )
        )

    yield from setup_ndattributes(spectroscopy_detector.roistat, params)  # type: ignore

    yield from load_settings(
        device=sample_stage,
        design_name="sample_stage_baseline",
        whitelist_pvs=[
            "x-acceleration_time",
            "x-velocity",
            "y-acceleration_time",
            "y-velocity",
        ],
    )

    spec = spec or Line(sample_stage.x, 0, 5, 5)  # type: ignore

    # write json-serializable form of spec into metadata
    if metadata is None:
        metadata = {}
    metadata["spec"] = serialize_spec(spec)

    if fly:
        LOGGER.info("Performing a fly scan.")
        yield from fly_scan(
            spec,
            spectroscopy_detector,
            pmac=pmac,
            pandabrick=pandabrick,
            exposure_time=exposure_time,
            detector_deadtime=MANTA_DETECTOR_DEADTIME,
            panda_whitelist=PANDA_WHITELIST,
            metadata=metadata,
        )
    else:
        LOGGER.info("Performing a step scan.")
        yield from spec_scan(
            {spectroscopy_detector, sample_stage},
            spec,  # type: ignore
            metadata=metadata,
        )


def demo_spectroscopy(
    spectroscopy_detector: AravisDetector = spectroscopy_detector,
    sample_stage: XYZStage = sample_stage,
    pmac: PmacIO = pmac,
    pandabrick: HDFPanda = pandabrick,
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

    velomax = grid_size / (xsteps * exposure_time)
    if velomax <= MAX_STAGE_VELOCITY * 0.98:
        fly = True
        LOGGER.info("test-rig-bluesky")
        LOGGER.info(
            f"Estimated velocity is {velomax:.2f} mm/sec, performing a fly scan."
        )
    else:
        fly = False
        LOGGER.info(
            f"Estimated velocity is {velomax:.2f} mm/sec, performing a step scan."
        )

    # Move to the start point
    # pmac should handle premove
    yield from bps.mv(
        *(sample_stage.x, xmin), *(sample_stage.y, ymin), group="initial_move"
    )

    grid = Line(sample_stage.y, ymin, ymax, ysteps) * ~Line(  # type: ignore
        sample_stage.x,  # type: ignore
        xmin,
        xmax,
        xsteps,
    )
    yield from spectroscopy(
        spectroscopy_detector=spectroscopy_detector,
        sample_stage=sample_stage,
        pmac=pmac,
        pandabrick=pandabrick,
        spec=grid,
        exposure_time=exposure_time,
        fly=fly,
        metadata=metadata,
    )
