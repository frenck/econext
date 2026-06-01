"""Binary sensor platform for ecoNEXT integration."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, get_alarm_name
from .cooling.entity import build_cooling_binary_sensors
from .coordinator import EconextCoordinator
from .entity import EconextEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ecoNEXT binary sensor entities from a config entry."""
    coordinator: EconextCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[BinarySensorEntity] = [EconextAlarmActiveBinarySensor(coordinator)]
    entities.extend(build_cooling_binary_sensors(coordinator))
    async_add_entities(entities)


class EconextAlarmActiveBinarySensor(EconextEntity, BinarySensorEntity):
    """Binary sensor that indicates if any alarm is currently active."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert"
    _attr_translation_key = "alarm_active"

    def __init__(self, coordinator: EconextCoordinator) -> None:
        """Initialize the alarm active binary sensor."""
        # Use a sentinel param_id since this entity reads from alarm data, not parameters
        super().__init__(coordinator, "_alarms", None)
        uid = coordinator.get_device_uid()
        self._attr_unique_id = f"{uid}_alarm_active"

    @property
    def is_on(self) -> bool | None:
        """Return True if any alarm is currently active (unresolved)."""
        return len(self.coordinator.active_alarms) > 0

    @property
    def extra_state_attributes(self) -> dict:
        """Return active alarm details."""
        active = self.coordinator.active_alarms
        return {
            "active_alarm_count": len(active),
            "active_alarm_codes": [{"code": a.get("code"), "name": get_alarm_name(a.get("code", 0))} for a in active],
        }

    def _is_value_valid(self) -> bool:
        """Alarm data is always valid if coordinator is updating."""
        return True
