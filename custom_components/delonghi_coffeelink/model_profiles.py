"""Per-model behaviour profiles for DeLonghi Coffee Link machines.

Different machine families speak slightly different dialects of the same Ayla
protocol. Rather than scatter ``if oem_model == ...`` checks across the codebase,
each family's differences live in one small class here:

- which binary command property it uses (informational; the coordinator still
  auto-detects the live one from a candidate list);
- whether commands are **synthesized** (PrimaDonna Soul) or **learned from the
  official app and replayed** (Eletta Explore and, by default, any unknown
  model - replay works for any machine once taught);
- how a beverage / wake command value is produced.

To add first-class support for a new model, add a ``ModelProfile`` subclass with
its ``matches()`` rule and (if it needs the learn-and-replay path) set
``learns_from_app = True``. Nothing else in the integration has to change.
"""
from __future__ import annotations

from .command_builder import build_and_encode, build_wake_encoded, replay_with_timestamp
from .const import ELETTA_OEM_PREFIX


class ModelProfile:
    """Base/default profile: synthesize fixed commands (PrimaDonna Soul style)."""

    key = "generic"
    label = "Generic Coffee Link"
    command_property = "data_request"
    # When True, the integration captures the official app's frames and replays
    # them verbatim instead of synthesizing them (reliable on models whose
    # command bytes differ from the reference Soul).
    learns_from_app = False

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return False

    def beverage_value(
        self, beverage_id: int, action: int, learned_frame: str | None
    ) -> str | None:
        """Return the base64 command value to send for a beverage.

        Returns ``None`` to signal "this profile needs a learned frame that is
        not available yet" - the caller then sends a best-effort frame and tells
        the user to teach it from the app.
        """
        return build_and_encode(beverage_id, action)

    def wake_value(self, learned_frame: str | None) -> str | None:
        """Return the base64 wake/power-on value, or ``None`` if a learned frame
        is required but not available yet."""
        return build_wake_encoded()


class SoulProfile(ModelProfile):
    """PrimaDonna Soul (``oem_model = DL-millcore``) - the reference model.

    Uses a fixed 18-byte beverage frame and a synthesized wake; both work out of
    the box, so there is nothing to learn.
    """

    key = "soul"
    label = "PrimaDonna Soul (DL-millcore)"
    command_property = "data_request"
    learns_from_app = False

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return oem_model.startswith("DL-millcore")


class ElettaProfile(ModelProfile):
    """Eletta Explore (``oem_model = DL-striker-cb``).

    Uses a variable-length beverage frame (recipe/quantity/intensity/milk encoded
    inline) and a wake frame carrying a per-device signature. The byte layout is
    not safely synthesizable, so the integration learns each frame from the
    official app and replays it verbatim with only a fresh timestamp.
    """

    key = "eletta"
    label = "Eletta Explore (DL-striker-cb)"
    command_property = "app_data_request"
    learns_from_app = True

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return oem_model.startswith(ELETTA_OEM_PREFIX)

    def beverage_value(
        self, beverage_id: int, action: int, learned_frame: str | None
    ) -> str | None:
        if learned_frame is not None:
            return replay_with_timestamp(learned_frame)
        return None

    def wake_value(self, learned_frame: str | None) -> str | None:
        if learned_frame is not None:
            return replay_with_timestamp(learned_frame)
        return None


# Most specific first; the generic default is applied explicitly in profile_for.
PROFILES: tuple[type[ModelProfile], ...] = (SoulProfile, ElettaProfile)


def profile_for(oem_model: str | None, command_property: str | None = None) -> ModelProfile:
    """Pick the behaviour profile for a device.

    Matches a known ``oem_model`` first. For an unknown model we default to the
    learn-and-replay (Eletta-style) behaviour - it works on any machine once
    taught - unless it looks Soul-like (the plain ``data_request`` channel), in
    which case the synthesized path is the safe choice.
    """
    oem = oem_model or ""
    for profile in PROFILES:
        if profile.matches(oem):
            return profile()
    if command_property == "data_request":
        return SoulProfile()
    return ElettaProfile()
