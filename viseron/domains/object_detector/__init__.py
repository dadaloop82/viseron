"""Object detector domain."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from queue import Empty, Queue
from typing import List

import voluptuous as vol

from viseron.components.data_stream import COMPONENT as DATA_STREAM_COMPONENT
from viseron.const import VISERON_SIGNAL_SHUTDOWN
from viseron.domains.camera.shared_frames import SharedFrame, SharedFrames
from viseron.helpers.filter import Filter
from viseron.helpers.schemas import COORDINATES_SCHEMA, MIN_MAX_SCHEMA
from viseron.watchdog.thread_watchdog import RestartableThread

from .const import (
    CONFIG_CAMERAS,
    CONFIG_COORDINATES,
    CONFIG_FPS,
    CONFIG_LABEL_CONFIDENCE,
    CONFIG_LABEL_HEIGHT_MAX,
    CONFIG_LABEL_HEIGHT_MIN,
    CONFIG_LABEL_LABEL,
    CONFIG_LABEL_POST_PROCESSOR,
    CONFIG_LABEL_REQUIRE_MOTION,
    CONFIG_LABEL_TRIGGER_RECORDER,
    CONFIG_LABEL_WIDTH_MAX,
    CONFIG_LABEL_WIDTH_MIN,
    CONFIG_LABELS,
    CONFIG_LOG_ALL_OBJECTS,
    CONFIG_MASK,
    CONFIG_MAX_FRAME_AGE,
    CONFIG_SCAN_ON_MOTION_ONLY,
    CONFIG_ZONE_NAME,
    CONFIG_ZONES,
    DATA_OBJECT_DETECTOR_RESULT,
    DEFAULT_FPS,
    DEFAULT_LABEL_CONFIDENCE,
    DEFAULT_LABEL_HEIGHT_MAX,
    DEFAULT_LABEL_HEIGHT_MIN,
    DEFAULT_LABEL_POST_PROCESSOR,
    DEFAULT_LABEL_REQUIRE_MOTION,
    DEFAULT_LABEL_TRIGGER_RECORDER,
    DEFAULT_LABEL_WIDTH_MAX,
    DEFAULT_LABEL_WIDTH_MIN,
    DEFAULT_LABELS,
    DEFAULT_LOG_ALL_OBJECTS,
    DEFAULT_MASK,
    DEFAULT_MAX_FRAME_AGE,
    DEFAULT_SCAN_ON_MOTION_ONLY,
    DEFAULT_ZONES,
    EVENT_OBJECTS_IN_FOV,
)
from .detected_object import DetectedObject
from .zone import Zone

DOMAIN = "object_detector"


def ensure_min_max(label: dict) -> dict:
    """Ensure min values are not larger than max values."""
    if label["height_min"] >= label["height_max"]:
        raise vol.Invalid("height_min may not be larger or equal to height_max")
    if label["width_min"] >= label["width_max"]:
        raise vol.Invalid("width_min may not be larger or equal to width_max")
    return label


LABEL_SCHEMA = vol.Schema(
    {
        vol.Required(CONFIG_LABEL_LABEL): str,
        vol.Optional(
            CONFIG_LABEL_CONFIDENCE, default=DEFAULT_LABEL_CONFIDENCE
        ): MIN_MAX_SCHEMA,
        vol.Optional(
            CONFIG_LABEL_HEIGHT_MIN, default=DEFAULT_LABEL_HEIGHT_MIN
        ): MIN_MAX_SCHEMA,
        vol.Optional(
            CONFIG_LABEL_HEIGHT_MAX, default=DEFAULT_LABEL_HEIGHT_MAX
        ): MIN_MAX_SCHEMA,
        vol.Optional(
            CONFIG_LABEL_WIDTH_MIN, default=DEFAULT_LABEL_WIDTH_MIN
        ): MIN_MAX_SCHEMA,
        vol.Optional(
            CONFIG_LABEL_WIDTH_MAX, default=DEFAULT_LABEL_WIDTH_MAX
        ): MIN_MAX_SCHEMA,
        vol.Optional(
            CONFIG_LABEL_TRIGGER_RECORDER, default=DEFAULT_LABEL_TRIGGER_RECORDER
        ): bool,
        vol.Optional(
            CONFIG_LABEL_REQUIRE_MOTION, default=DEFAULT_LABEL_REQUIRE_MOTION
        ): bool,
        vol.Optional(
            CONFIG_LABEL_POST_PROCESSOR, default=DEFAULT_LABEL_POST_PROCESSOR
        ): vol.Any(str, None),
    },
    ensure_min_max,
)

ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONFIG_ZONE_NAME): str,
        vol.Required(CONFIG_COORDINATES): COORDINATES_SCHEMA,
        vol.Optional(CONFIG_LABELS): [LABEL_SCHEMA],
    }
)

CAMERA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONFIG_FPS, default=DEFAULT_FPS): vol.All(
            vol.Any(float, int), vol.Coerce(float), vol.Range(min=0.0)
        ),
        vol.Optional(
            CONFIG_SCAN_ON_MOTION_ONLY, default=DEFAULT_SCAN_ON_MOTION_ONLY
        ): bool,
        vol.Optional(CONFIG_LABELS, default=DEFAULT_LABELS): vol.Any(
            [], [LABEL_SCHEMA]
        ),
        vol.Optional(CONFIG_MAX_FRAME_AGE, default=DEFAULT_MAX_FRAME_AGE): vol.All(
            vol.Any(float, int), vol.Coerce(float), vol.Range(min=0.0)
        ),
        vol.Optional(CONFIG_LOG_ALL_OBJECTS, default=DEFAULT_LOG_ALL_OBJECTS): bool,
        vol.Optional(CONFIG_MASK, default=DEFAULT_MASK): [
            {vol.Required(CONFIG_COORDINATES): COORDINATES_SCHEMA}
        ],
        vol.Optional(CONFIG_ZONES, default=DEFAULT_ZONES): vol.Any(
            [],
            [ZONE_SCHEMA],
        ),
    }
)

BASE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONFIG_CAMERAS): {str: CAMERA_SCHEMA},
    }
)


class AbstractObjectDetector(ABC):
    """Abstract Object Detector."""

    def __init__(self, vis, config, camera_identifier):
        self._vis = vis
        self._config = config
        self._camera_identifier = camera_identifier
        self._logger = logging.getLogger(f"{self.__module__}.{camera_identifier}")

        self._shared_frames = SharedFrames()

        self._objects_in_fov: List[DetectedObject] = []
        self.object_filters = {}
        if config[CONFIG_CAMERAS][camera_identifier][CONFIG_LABELS]:
            for object_filter in config[CONFIG_CAMERAS][camera_identifier][
                CONFIG_LABELS
            ]:
                self.object_filters[object_filter[CONFIG_LABEL_LABEL]] = Filter(
                    vis.get_registered_camera(camera_identifier).resolution,
                    object_filter,
                    config[CONFIG_CAMERAS][camera_identifier][CONFIG_MASK],
                )

        self.zones: List[Zone] = []
        for zone in config[CONFIG_CAMERAS][camera_identifier][CONFIG_ZONES]:
            self.zones.append(Zone(vis, config, camera_identifier, zone))

        if not self.zones and not self.object_filters:
            self._logger.warning(
                "No labels or zones configured. No objects will be detected"
            )

        self._kill_received = False
        self.object_detection_queue: Queue[SharedFrame] = Queue(maxsize=10)
        self._object_detection_thread = RestartableThread(
            target=self._object_detection,
            name=f"{camera_identifier}.object_detection",
            register=True,
            daemon=True,
        )
        self._object_detection_thread.start()
        vis.register_signal_handler(VISERON_SIGNAL_SHUTDOWN, self.stop)

    def filter_fov(self, shared_frame: SharedFrame, objects: List[DetectedObject]):
        """Filter field of view."""
        objects_in_fov = []
        for obj in objects:
            if self.object_filters.get(obj.label) and self.object_filters[
                obj.label
            ].filter_object(obj):
                obj.relevant = True
                objects_in_fov.append(obj)

                if self.object_filters[obj.label].trigger_recorder:
                    obj.trigger_recorder = True

        self.objects_in_fov_setter(shared_frame, objects_in_fov)
        if self._config[CONFIG_CAMERAS][self._camera_identifier][
            CONFIG_LOG_ALL_OBJECTS
        ]:
            self._logger.debug(
                "All objects: %s",
                [obj.formatted for obj in objects],
            )
        else:
            self._logger.debug(
                "Objects: %s", [obj.formatted for obj in self.objects_in_fov]
            )

    @property
    def objects_in_fov(self):
        """Return all objects in field of view."""
        return self._objects_in_fov

    def objects_in_fov_setter(
        self, shared_frame: SharedFrame, objects: List[DetectedObject]
    ):
        """Set objects in field of view."""
        if objects == self._objects_in_fov:
            return

        self._objects_in_fov = objects
        self._vis.dispatch_event(
            EVENT_OBJECTS_IN_FOV.format(camera_identifier=self._camera_identifier),
            {
                "camera_identifier": self._camera_identifier,
                "shared_frame": shared_frame,
                "objects": objects,
            },
        )

    def filter_zones(self, shared_frame: SharedFrame, objects: List[DetectedObject]):
        """Filter all zones."""
        for zone in self.zones:
            zone.filter_zone(shared_frame, objects)

    @abstractmethod
    def preprocess(self, frame):
        """Perform preprocessing of frame before running detection."""

    def _object_detection(self):
        """Perform object detection and publish the results."""
        while not self._kill_received:
            try:
                shared_frame: SharedFrame = self.object_detection_queue.get(timeout=1)
            except Empty:
                continue
            decoded_frame = self._shared_frames.get_decoded_frame_rgb(shared_frame)
            preprocessed_frame = self.preprocess(decoded_frame)

            if (frame_age := time.time() - shared_frame.capture_time) > self._config[
                CONFIG_CAMERAS
            ][shared_frame.camera_identifier][CONFIG_MAX_FRAME_AGE]:
                self._logger.debug(f"Frame is {frame_age} seconds old. Discarding")
                self._shared_frames.close(shared_frame)
                continue

            objects = self.return_objects(preprocessed_frame)
            self.filter_fov(shared_frame, objects)
            self.filter_zones(shared_frame, objects)
            self._vis.data[DATA_STREAM_COMPONENT].publish_data(
                DATA_OBJECT_DETECTOR_RESULT.format(
                    camera_identifier=shared_frame.camera_identifier
                ),
                self.objects_in_fov,
            )
            self._shared_frames.close(shared_frame)
        self._logger.debug("Object detection thread stopped")

    @abstractmethod
    def return_objects(self, frame):
        """Perform object detection."""

    @property
    @abstractmethod
    def fps(self) -> str:
        """Return object detector fps."""

    @property
    def scan_on_motion_only(self):
        """Return if scanning should only be done when there is motion."""
        return self._config[CONFIG_CAMERAS][self._camera_identifier][
            CONFIG_SCAN_ON_MOTION_ONLY
        ]

    def stop(self):
        """Stop object detector."""
        self._kill_received = True
        self._object_detection_thread.join()