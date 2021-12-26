"""Home Assistant MQTT binary sensor."""
from __future__ import annotations

from .entity import HassMQTTEntity

DOMAIN = "binary_sensor"


class HassMQTTBinarySensor(HassMQTTEntity):
    """Base class for all Home Assistant MQTT binary sensors."""

    # These should NOT be overridden.
    domain = DOMAIN

    @property
    def config_payload(self):
        """Return config payload."""
        payload = super().config_payload
        payload["payload_on"] = "on"
        payload["payload_off"] = "off"

        if self._entity.device_class:
            payload["device_class"] = self._entity.device_class
        return payload