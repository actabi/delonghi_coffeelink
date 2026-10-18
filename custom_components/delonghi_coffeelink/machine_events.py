"""Derive machine events from successive polls (pure; no Home Assistant import).

The cloud gives a snapshot, never a stream, so an "event" here is a difference
between two snapshots: the machine status moved, an alarm bit appeared or went
away, or a per-drink lifetime counter went up. ``event.py`` turns the list this
module returns into Home Assistant event-entity events.

Two rules keep it from inventing events:

* the first snapshot is a baseline and produces nothing - a restart must not
  announce every alarm that was already active, or every drink ever brewed;
* a value that is unknown in the new snapshot (stale or undecodable monitor,
  counter missing from the poll) is not a change. The last known value is kept
  and compared again once a real one arrives, so a status that moved while the
  monitor was stale is still reported, just late.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    BEVERAGE_COUNTERS,
    EVENT_ALARM_CLEARED,
    EVENT_ALARM_RAISED,
    EVENT_BEVERAGE_DISPENSED,
    EVENT_STATUS_CHANGED,
    alarm_names,
)
from .counters import parse_counter_value


@dataclass(frozen=True)
class Snapshot:
    """What one poll says about the machine; ``None`` means "not known"."""

    status: str | None = None
    alarms: frozenset[str] | None = None
    counters: dict[str, int] = field(default_factory=dict)


def take_snapshot(
    data: dict[str, Any] | None, monitor: dict[str, Any] | None, *, stale: bool
) -> Snapshot:
    """Build a snapshot from the coordinator's properties and decoded monitor."""
    status: str | None = None
    alarms: frozenset[str] | None = None
    monitor = monitor or {}
    if monitor and "error" not in monitor and not stale:
        status = monitor.get("status_name")
        if "alarms" in monitor:
            alarms = alarm_names(monitor["alarms"])
    counters: dict[str, int] = {}
    for key, prop_name in BEVERAGE_COUNTERS.items():
        prop = (data or {}).get(prop_name)
        if not isinstance(prop, dict):
            continue
        value = parse_counter_value(prop.get("value"))
        if isinstance(value, int):
            counters[key] = value
    return Snapshot(status=status, alarms=alarms, counters=counters)


def merge(previous: Snapshot | None, new: Snapshot) -> Snapshot:
    """The snapshot to compare the NEXT poll against: unknowns keep the old value."""
    if previous is None:
        return new
    return Snapshot(
        status=new.status if new.status is not None else previous.status,
        alarms=new.alarms if new.alarms is not None else previous.alarms,
        counters={**previous.counters, **new.counters},
    )


def diff(previous: Snapshot | None, new: Snapshot) -> list[tuple[str, dict[str, Any]]]:
    """Events between two snapshots, as ``(event_type, attributes)`` pairs.

    Ordered status, then alarms raised, alarms cleared, then drinks - and each
    group sorted - so the same pair of polls always yields the same sequence.
    """
    if previous is None:
        return []
    events: list[tuple[str, dict[str, Any]]] = []
    if (
        previous.status is not None
        and new.status is not None
        and new.status != previous.status
    ):
        events.append(
            (EVENT_STATUS_CHANGED, {"from_status": previous.status, "to_status": new.status})
        )
    if previous.alarms is not None and new.alarms is not None:
        for name in sorted(new.alarms - previous.alarms):
            events.append((EVENT_ALARM_RAISED, {"alarm": name}))
        for name in sorted(previous.alarms - new.alarms):
            events.append((EVENT_ALARM_CLEARED, {"alarm": name}))
    for key in sorted(new.counters):
        before = previous.counters.get(key)
        after = new.counters[key]
        # A drop is a counter reset (or a bad read), never a drink.
        if before is not None and after > before:
            events.append(
                (
                    EVENT_BEVERAGE_DISPENSED,
                    {"beverage": key, "count": after - before, "total": after},
                )
            )
    return events
