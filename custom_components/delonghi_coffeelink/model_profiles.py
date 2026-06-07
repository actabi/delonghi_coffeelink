"""Per-model behaviour profiles for DeLonghi Coffee Link machines."""
from __future__ import annotations

from .command_builder import (
    build_and_encode,
    build_standby_with_app_id_encoded,
    build_wake_encoded,
    build_wake_with_app_id_encoded,
    is_valid_wake_frame,
    replay_with_timestamp,
)
from .const import DEFAULT_CLOUD_APP_ID, ELETTA_OEM_PREFIX


class ModelProfile:
    """Base/default profile: synthesize fixed commands (PrimaDonna Soul style)."""

    key = "generic"
    label = "Generic Coffee Link"
    command_property = "data_request"
    learns_from_app = False
    uses_cloud_session = False

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return False

    def beverage_value(
        self,
        beverage_id: int,
        action: int,
        learned_frame: str | None,
        *,
        cloud_app_id: int = DEFAULT_CLOUD_APP_ID,
    ) -> str | None:
        return build_and_encode(beverage_id, action)

    def wake_value(
        self,
        learned_frame: str | None,
        *,
        cloud_app_id: int = DEFAULT_CLOUD_APP_ID,
    ) -> str | None:
        return build_wake_encoded()

    def standby_value(self, *, cloud_app_id: int = DEFAULT_CLOUD_APP_ID) -> str | None:
        return None


class SoulProfile(ModelProfile):
    """PrimaDonna Soul (``oem_model = DL-millcore``)."""

    key = "soul"
    label = "PrimaDonna Soul (DL-millcore)"
    command_property = "data_request"
    learns_from_app = False

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return oem_model.startswith("DL-millcore")


class ElettaProfile(ModelProfile):
    """Eletta Explore (``oem_model = DL-striker-cb``).

    Beverages use learn-and-replay from the official app. Wake/standby use
    synthesized cloud frames with an app_id tail after ``app_device_connected``.
    """

    key = "eletta"
    label = "Eletta Explore (DL-striker-cb)"
    command_property = "app_data_request"
    learns_from_app = True
    uses_cloud_session = True

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return oem_model.startswith(ELETTA_OEM_PREFIX)

    def beverage_value(
        self,
        beverage_id: int,
        action: int,
        learned_frame: str | None,
        *,
        cloud_app_id: int = DEFAULT_CLOUD_APP_ID,
    ) -> str | None:
        if learned_frame is not None:
            return replay_with_timestamp(learned_frame)
        return None

    def wake_value(
        self,
        learned_frame: str | None,
        *,
        cloud_app_id: int = DEFAULT_CLOUD_APP_ID,
    ) -> str | None:
        if learned_frame and is_valid_wake_frame(learned_frame):
            return replay_with_timestamp(learned_frame)
        return build_wake_with_app_id_encoded(app_id=cloud_app_id)

    def standby_value(self, *, cloud_app_id: int = DEFAULT_CLOUD_APP_ID) -> str | None:
        return build_standby_with_app_id_encoded(app_id=cloud_app_id)


PROFILES: tuple[type[ModelProfile], ...] = (SoulProfile, ElettaProfile)


def profile_for(oem_model: str | None, command_property: str | None = None) -> ModelProfile:
    oem = oem_model or ""
    for profile in PROFILES:
        if profile.matches(oem):
            return profile()
    if command_property == "data_request":
        return SoulProfile()
    return ElettaProfile()
