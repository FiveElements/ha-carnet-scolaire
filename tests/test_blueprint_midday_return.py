"""The "Retour du midi" blueprint, run by Home Assistant itself.

A blueprint is code nobody executes until a parent's announcement fails to go
out, and this one carries the only templates of an automation: the wait for
the scheduled end and the message. So each language is instantiated through
the real ``automation`` and ``blueprint`` integrations, against a sensor that
publishes what ``sensor.py:_morning_end_attributes`` publishes, and the clock
is moved past the trigger.

The day is the one that motivated the blueprint: the morning ends at 10:30
because the 10:30 lesson is cancelled, it was scheduled to end at 11:30, and
lessons resume at 14:00.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import pathlib
import shutil
from typing import TYPE_CHECKING, Any

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import (
    async_capture_events,
    async_fire_time_changed,
)

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

pytestmark = REQUIRES_HASS

BLUEPRINTS = pathlib.Path(__file__).parent.parent / "blueprints" / "automation"
SENSOR = "sensor.enfant_un_end_of_morning"
EVENT = "carnet_scolaire_test_midday"

#: The frozen day, in Home Assistant's own timezone.
PARIS = dt_util.get_time_zone("Europe/Paris")


def _at(hour: int, minute: int = 0) -> datetime:
    """An instant on the frozen day."""
    return datetime(2026, 3, 12, hour, minute, tzinfo=PARIS)


def _install(language: str, target: pathlib.Path) -> None:
    """Copy one language's blueprint where Home Assistant looks for it."""
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        BLUEPRINTS / "carnet_scolaire" / language / "midday_return.yaml",
        target / "midday_return.yaml",
    )


async def _run(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    language: str,
    *,
    include_cancellations: bool,
    scheduled_end: datetime | None,
    until: datetime,
) -> list[Any]:
    """Instantiate the blueprint, then let the clock run to ``until``."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to(_at(9))
    await hass.async_add_executor_job(
        _install,
        language,
        pathlib.Path(hass.config.path("blueprints", "automation", "carnet_scolaire")),
    )
    hass.states.async_set(
        SENSOR,
        _at(10, 30).isoformat(),
        {
            "device_class": "timestamp",
            "subject": "Mathématiques",
            "scheduled_end": scheduled_end.isoformat() if scheduled_end else None,
            "canceled_before_break": 1,
            "resumes_at": _at(14).isoformat(),
            "break_minutes": 210,
        },
    )
    events = async_capture_events(hass, EVENT)

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "carnet_scolaire/midday_return.yaml",
                    "input": {
                        "morning_end_sensor": SENSOR,
                        "include_cancellations": include_cancellations,
                        "midday_action": [
                            {
                                "event": EVENT,
                                "event_data": {"message": "{{ message }}"},
                            }
                        ],
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()

    now = _at(9)
    while now < until:
        now += timedelta(minutes=15)
        await _tick(hass, freezer, now)
    return events


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, now: datetime
) -> None:
    """Move the clock and let the loop run.

    Not ``async_block_till_done``: it waits for every running automation,
    and a run that is waiting for the scheduled end only finishes once the
    clock is moved again -- which that call would never let happen.
    """
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.parametrize("language", ["fr", "en"])
async def test_the_early_end_is_announced_when_cancellations_count(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, language: str
) -> None:
    """At 10:30, the hour the child actually leaves, with the cancellation."""
    events = await _run(
        hass,
        freezer,
        language,
        include_cancellations=True,
        scheduled_end=_at(11, 30),
        until=_at(10, 45),
    )

    assert len(events) == 1
    message = events[0].data["message"]
    assert "10:30" in message
    assert "14:00" in message
    assert "1" in message


@pytest.mark.parametrize("language", ["fr", "en"])
async def test_a_child_who_stays_is_announced_at_the_scheduled_end(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, language: str
) -> None:
    """Nothing at 10:30; the announcement waits for the 11:30 on the grid.

    The trigger still fires at the sensor's state -- no native trigger can
    aim at an attribute -- so the run waits the difference, and must not act
    before it is over.
    """
    events = await _run(
        hass,
        freezer,
        language,
        include_cancellations=False,
        scheduled_end=_at(11, 30),
        until=_at(11, 15),
    )
    assert events == []

    await _tick(hass, freezer, _at(11, 31))

    assert len(events) == 1
    assert "11:30" in events[0].data["message"]


@pytest.mark.parametrize("language", ["fr", "en"])
async def test_no_planned_break_means_no_homecoming_for_a_child_who_stays(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, language: str
) -> None:
    """Only cancellations opened this break; the grid kept the child all day."""
    events = await _run(
        hass,
        freezer,
        language,
        include_cancellations=False,
        scheduled_end=None,
        until=_at(12),
    )

    assert events == []
