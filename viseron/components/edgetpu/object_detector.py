"""EdgeTPU Object detector."""
import logging
from queue import Queue
from typing import List

import cv2

from viseron import Viseron
from viseron.domains.object_detector import (
    CONFIG_CAMERAS,
    CONFIG_FPS,
    DOMAIN,
    AbstractObjectDetector,
)
from viseron.domains.object_detector.detected_object import DetectedObject

from .const import COMPONENT, CONFIG_OBJECT_DETECTOR

LOGGER = logging.getLogger(__name__)


def setup(vis: Viseron, config):
    """Set up the edgetpu object_detector domain."""
    for camera_identifier in config[CONFIG_OBJECT_DETECTOR][CONFIG_CAMERAS].keys():
        vis.wait_for_camera(
            camera_identifier,
        )
        ObjectDetector(vis, config[DOMAIN], camera_identifier)

    return True


class ObjectDetector(AbstractObjectDetector):
    """Performs object detection."""

    def __init__(self, vis: Viseron, config, camera_identifier):
        self._vis = vis
        self._config = config
        self._camera_identifier = camera_identifier

        self._edgetpu = vis.data[COMPONENT]
        self._object_result_queue = Queue(maxsize=1)

        super().__init__(vis, config, camera_identifier)

        self._vis.register_object_detector(camera_identifier, self)

    def preprocess(self, frame):
        """Return preprocessed frame before performing object detection."""
        frame = cv2.resize(
            frame,
            (self.model_width, self.model_height),
            interpolation=cv2.INTER_LINEAR,
        )

        return frame.reshape(1, frame.shape[0], frame.shape[1], frame.shape[2])

    def return_objects(self, frame) -> List[DetectedObject]:
        """Perform object detection."""
        return self._edgetpu.invoke(
            frame, self._camera_identifier, self._object_result_queue
        )

    @property
    def fps(self):
        """Return object detector fps."""
        return self._config[CONFIG_CAMERAS][self._camera_identifier][CONFIG_FPS]

    @property
    def model_width(self) -> int:
        """Return trained model width."""
        return self._edgetpu.model_width

    @property
    def model_height(self) -> int:
        """Return trained model height."""
        return self._edgetpu.model_height