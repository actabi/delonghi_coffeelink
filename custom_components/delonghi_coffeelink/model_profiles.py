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

import time

from .command_builder import (
    build_and_encode,
    build_standby_encoded,
    build_wake_encoded,
    replay_with_timestamp,
)
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
    # ECAM models (Eletta / app_* channel) require a cloud session via
    # app_device_connected before commands are relayed; Soul does not.
    uses_cloud_session = False
    # When True the coordinator rewrites the connected property on a timer so the
    # machine keeps publishing its monitor datapoint (issue #14). This is a
    # different concern from uses_cloud_session: that one gates *commands* and is
    # driven on demand, this one keeps *status reads* alive and must be periodic.
    keeps_monitor_session = False

    def monitor_session_value(self) -> object | None:
        """Payload written to the connected property to refresh the session.

        ``None`` means "this profile has no keepalive". The Soul's
        ``device_connected`` holds a plain unix timestamp - NOT the
        ``base64(timestamp + app_id)`` blob that ECAM's ``app_device_connected``
        takes (see ayla_client.async_post_cloud_session), which is why the two
        paths cannot share one payload builder.
        """
        return None

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

    def standby_value(self, signature: bytes | None) -> str | None:
        """Return the base64 standby/power-off value.

        Always synthesized (the official app exposes no power-off control, so
        there is nothing to learn). ``signature`` is the 4-byte device
        signature extracted from a learned frame, for models that require it;
        the synthesized Soul path ignores it.
        """
        return build_standby_encoded()


class SoulProfile(ModelProfile):
    """PrimaDonna Soul (``oem_model = DL-millcore``) - the reference model.

    Uses a fixed 18-byte beverage frame and a synthesized wake; both work out of
    the box, so there is nothing to learn.
    """

    key = "soul"
    label = "PrimaDonna Soul (DL-millcore)"
    command_property = "data_request"
    learns_from_app = False
    keeps_monitor_session = True

    @classmethod
    def matches(cls, oem_model: str) -> bool:
        return oem_model.startswith("DL-millcore")

    def monitor_session_value(self) -> object | None:
        return int(time.time())


class GenericSoulProfile(SoulProfile):
    """A machine that speaks the Soul dialect but is not a known Soul.

    ``profile_for`` falls back here for any unrecognised ``oem_model`` on the
    plain ``data_request`` channel, and the only thing this changes is that the
    monitor keepalive stays off. The keepalive writes a bare unix timestamp to
    whichever ``CONNECTED_PROPERTY_CANDIDATES`` entry the device exposes, and
    that payload shape is confirmed on ``DL-millcore`` and nowhere else - the
    Eletta family's ``app_device_connected`` takes ``base64(timestamp +
    signed_app_id)`` instead, and a machine we have never seen may take a third
    thing. Writing the wrong shape into the property the official app uses to
    register its own session is not a guess worth making on someone else's
    machine, and doing it every 15 s is not a guess worth repeating.

    Everything else - the fixed 18-byte beverage frame, the synthesized wake -
    is unchanged, so an unknown Soul-like machine keeps working exactly as it
    did before the keepalive existed.
    """

    key = "soul-generic"
    label = "Soul-style machine (unrecognised model)"
    keeps_monitor_session = False


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
    uses_cloud_session = True

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

    def standby_value(self, signature: bytes | None) -> str | None:
        """Standby is synthesized even on learn-and-replay models (the app has
        no power-off control to capture), but the Eletta ignores power frames
        without the per-device signature - so it is appended from any learned
        frame. ``None`` until a frame carrying the signature has been learned.
        """
        if signature is None:
            return None
        return build_standby_encoded(signature)


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
        # Deliberately not SoulProfile: same command dialect, but the monitor
        # keepalive is confirmed on DL-millcore only. See GenericSoulProfile.
        return GenericSoulProfile()
    return ElettaProfile()
