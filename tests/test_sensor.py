"""The sensor attributes a dashboard actually reads.

Most of this platform is a table of descriptions, and a table is not worth a
test per row. What is worth testing is the handful of places where the shape of
an attribute set -- not its value -- is what a card depends on, because those
are the failures that reach a parent as a blank tile with no error anywhere.

Two of them live here.

**A menu says whether there is a menu.** The attributes were absent entirely on
a day the canteen published nothing, so a template reading ``main_meal`` got
``None`` -- exactly what it gets before the tier has ever run. "No lunch today"
and "we have not looked yet" are different sentences to say to a parent, and
with no key to read there was no way to say either.

**A homework description is prose.** ``descriptif`` is HTML, and a card can
neither print it nor inject it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.carnet_scolaire.const import Tier

from .conftest import CHILDREN, PARIS, REQUIRES_HASS
from .fixtures import protocol
from .fixtures.client import FakeClient

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount

pytestmark = REQUIRES_HASS

#: The courses the shared fake canteen serves, and the keys a card reads them
#: from. Listed once so the "nothing published" case can assert the *same* set
#: rather than a hand-copied subset that would drift away from it.
COURSE_KEYS = (
    "first_meal",
    "main_meal",
    "side_meal",
    "other_meal",
    "cheese",
    "dessert",
)


def _attributes(hass: HomeAssistant, entity_id: str) -> dict[str, object]:
    """One sensor's attributes, read the way a template reads them."""
    state = hass.states.get(entity_id)
    assert state is not None, f"no entity {entity_id}"
    return dict(state.attributes)


async def test_a_published_menu_carries_its_courses_and_says_it_is_published(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The ordinary day, which is the baseline the empty one is compared to."""
    attributes = _attributes(hass, "sensor.enfant_un_menu_today")

    assert attributes["published"] is True
    assert attributes["main_meal"] == ["Poisson pané"]
    assert attributes["is_lunch"] is True


class TestWhenTheCanteenPublishesNothing:
    """A day with no menu at all -- a holiday, or a school with no canteen."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """The shared client, with the canteen silent.

        Overridden here and not in ``conftest.py`` because a fake server that
        serves no menus would make every other module's menu assertions
        vacuous rather than failing them.
        """
        return FakeClient(children=CHILDREN, menus_published=False)

    async def test_the_keys_are_still_there(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Empty lists, not absent keys.

        This is the defect. ``state_attr(entity, 'main_meal')`` returned
        ``None`` here and ``None`` again before the first collection, so a card
        could not distinguish them and showed nothing in both cases. With the
        keys present and empty, "no menu" is renderable.
        """
        attributes = _attributes(hass, "sensor.enfant_un_menu_today")

        assert attributes["published"] is False
        for key in COURSE_KEYS:
            assert attributes[key] == [], key
        # Not guessed: there is no meal, so there is no service to report.
        assert attributes["is_lunch"] is None

    async def test_the_state_is_still_unknown_rather_than_zero(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """``published: False`` must not have turned into "a menu of nothing".

        The state is the dish count, and zero dishes is a claim about a menu
        that exists. The attributes gained a shape; the state keeps saying
        there is nothing to count.
        """
        state = hass.states.get("sensor.enfant_un_menu_today")

        assert state is not None
        assert state.state == "unknown"


class TestHomeworkAttributes:
    """The homework list a card renders, rather than the to-do entity."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One item, described the way PRONOTE describes it: in HTML."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(description="<p>Exercice 3 p.&nbsp;52</p>")]
        )
        return client

    async def test_a_card_gets_the_prose_and_the_markup(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Both, because neither one substitutes for the other.

        A card that prints ``description`` shows the tags; a card that injects
        it hands every teacher's text field a route into the dashboard. So the
        printable form travels next to it, converted once here rather than
        three times in three cards.
        """
        attributes = _attributes(hass, "sensor.enfant_un_homework_to_do")
        item = attributes["items"][0]  # type: ignore[index]

        assert item["description_text"] == "Exercice 3 p. 52"
        assert item["description"] == "<p>Exercice 3 p.&nbsp;52</p>"


class TestTheDiagnosticReadingsRefreshOnTheirOwn:
    """The eight limiter readings, and what they were actually doing: nothing.

    They all carried ``_attr_should_poll = True``, and it was inert --
    ``BaseCoordinatorEntity`` declares ``should_poll`` as a property returning
    ``False``, which shadows the attribute. So a tile called "next collection"
    was refreshed only when a collection happened, which is precisely the
    circumstance in which it is wrong to trust it: a tier due and failing left
    the timestamp frozen while the clock moved on, and a dashboard read "next
    collection: 40 minutes ago".
    """

    async def test_a_reading_is_rewritten_without_any_collection(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        parent_client: FakeClient,
        school_day: FrozenDateTimeFactory,
    ) -> None:
        """Time passes, the state is rewritten, and PRONOTE is not asked.

        Both halves matter and the second is the reason the first was hard.
        ``CoordinatorEntity.async_update`` calls ``async_request_refresh``, so
        simply switching polling on would have bought a fresh timestamp at the
        price of a server request every thirty seconds -- the one thing the
        budget exists to prevent (§7.1). The override is a no-op body, and this
        asserts the ledger stays where it was.
        """
        entity_id = "sensor.college_d_essai_session_age"
        before = hass.states.get(entity_id)
        assert before is not None, "no diagnostic sensor"
        posts = len(parent_client.posts)

        school_day.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done()

        after = hass.states.get(entity_id)
        assert after is not None
        assert after.last_reported > before.last_reported
        assert len(parent_client.posts) == posts

    async def test_a_missed_deadline_is_named_rather_than_left_to_be_guessed(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A timestamp in the past is a diagnosis only if it says what is late.

        ``overdue_by`` and ``failing`` are here because a deadline in the past
        looks exactly like a sensor that stopped updating, and a reader has to
        be able to tell those apart from the tile alone.
        """
        state = hass.states.get("sensor.college_d_essai_next_collection")

        assert state is not None
        assert "overdue_by" in state.attributes
        assert "failing" in state.attributes

    async def test_a_refresh_press_can_be_told_from_a_refresh_that_was_refused(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """ "Did my press land?" answered on the tile, not only in a dump.

        A button that can only say "the request was sent" is a button somebody
        presses twice, and a second press is not free on a tier that is
        failing. The scheduler always knew the answer -- ``boosted`` and the
        served-boost instant -- but it reached exactly two places, the
        downloadable diagnostics and a response-only service, neither of which
        a dashboard can read. So the information existed and the question could
        not be answered, which is the same shape of gap as ``deferrals``
        answering "did the limiter refuse" when the question was "did my
        request land".

        Asserted through the entity, deliberately: a test reading
        ``account.scheduler`` would pass against the exact state this replaces,
        where the fields were present and unreachable.
        """
        account.scheduler.request([Tier.MARKS])
        entity = hass.data["entity_components"]["sensor"].get_entity(
            "sensor.college_d_essai_next_collection"
        )
        assert entity is not None
        await entity.async_update_ha_state()

        state = hass.states.get("sensor.college_d_essai_next_collection")
        assert state is not None
        assert "marks" in state.attributes["boosted"], (
            "an armed boost has to be visible to whoever pressed the button"
        )
        assert "boost_served_at" in state.attributes


def _day(hour: int, minute: int = 0) -> datetime:
    """A naive instant on the frozen school day, as the protocol sends them."""
    return datetime(2026, 3, 12, hour, minute)


class TestWhenTheMorningEnds:
    """`morning_end` answers "when do I collect the child for lunch?".

    The state is a timestamp, so an automation triggers on it directly and
    needs no template walking `attributes.lessons` -- which is the §1 test any
    new entity has to pass.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """A day with a real lunch break: 8-12, then 14-16."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(14),
                    end=_day(16),
                    place=12,
                    duration=4,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_the_state_is_the_end_of_the_last_morning_lesson(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A timestamp, and the context a card needs beside it."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        # Compared as an instant, not as text: Home Assistant normalises a
        # timestamp state to UTC, so the establishment's 12:00+01:00 is stored
        # as 11:00+00:00 and a string comparison tests the serialisation
        # rather than the rule.
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 12, 0, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"
        assert state.attributes["resumes_at"] == "2026-03-12T14:00:00+01:00"
        assert state.attributes["break_minutes"] == 120
        # Nothing cancelled: the schedule and the day agree.
        assert state.attributes["scheduled_end"] == "2026-03-12T12:00:00+01:00"
        assert state.attributes["canceled_before_break"] == 0


def _lesson(
    identifier: str,
    start: tuple[int, int],
    end: tuple[int, int],
    subject: str,
    *,
    canceled: bool = False,
) -> dict[str, Any]:
    """One lesson on the frozen day, its slot derived from half-hours at 08:00."""
    place = (start[0] - 8) * 2 + start[1] // 30
    duration = ((end[0] - start[0]) * 60 + end[1] - start[1]) // 30
    return protocol.lesson(
        identifier=identifier,
        start=_day(*start),
        end=_day(*end),
        place=place,
        duration=duration,
        subject=subject,
        canceled=canceled,
    )


class TestWhenAnAbsentTeacherBringsTheMorningForward:
    """The lunch homecoming comes early, and it must still be announced."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """08:30-10:30, the 10:30 lesson cancelled, lunch, then 14:00-16:00.

        The shape of a real day on a live instance: the teacher of the last
        morning lesson was absent. The gap the child actually has starts at
        10:30 -- before the window -- so the rule that looked only at that gap
        published nothing, and the midday announcement never went out.
        """
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                _lesson("LESSON-A", (8, 30), (9, 30), "Français"),
                _lesson("LESSON-B", (9, 30), (10, 30), "Mathématiques"),
                _lesson(
                    "LESSON-C", (10, 30), (11, 30), "Physique-chimie", canceled=True
                ),
                _lesson("LESSON-D", (14, 0), (16, 0), "Histoire"),
            ]
        )
        return client

    async def test_the_morning_ends_at_the_last_lesson_that_takes_place(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """10:30, and the scheduled 11:30 beside it for a child who stays.

        The planned grid has its lunch break at 11:30, inside the window, and
        that is what makes the 10:30 gap a lunch break rather than "one
        morning lesson and a late afternoon". ``scheduled_end`` is what an
        automation waits for when the child stays at school while the teacher
        is absent.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 10, 30, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"
        assert state.attributes["scheduled_end"] == "2026-03-12T11:30:00+01:00"
        assert state.attributes["canceled_before_break"] == 1
        assert state.attributes["resumes_at"] == "2026-03-12T14:00:00+01:00"
        assert state.attributes["break_minutes"] == 210


class TestWhenCancellationsOpenAGapTheTimetableNeverHad:
    """A continuous day with its late morning cancelled."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """08:00-11:00, 11:00-14:00 cancelled, then 14:00-16:00."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                _lesson("LESSON-A", (8, 0), (11, 0), "Français"),
                _lesson("LESSON-B", (11, 0), (14, 0), "Sport", canceled=True),
                _lesson("LESSON-C", (14, 0), (16, 0), "Histoire"),
            ]
        )
        return client

    async def test_the_break_is_published_but_no_scheduled_end_is_invented(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The child really is free from 11:00; the grid planned no lunch.

        The state keeps the rule it always had -- the gap starts at 11:00 and
        is three hours long -- but ``scheduled_end`` is ``None``: a child who
        stays at school when a teacher is absent is at school until 16:00 on
        this day, and naming an instant would send somebody to collect them.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 11, 0, tzinfo=PARIS
        )
        assert state.attributes["scheduled_end"] is None
        assert state.attributes["canceled_before_break"] == 1


class TestWhenThereIsNoMiddayBreak:
    """Nothing, rather than a plausible wrong hour."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One morning lesson and one late-afternoon lesson.

        This is the day that makes "the day's largest gap" the wrong rule: the
        gap is five hours and it starts at 10:00, which is not a lunch break
        and would send a parent out mid-morning.
        """
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(9),
                    end=_day(10),
                    place=2,
                    duration=2,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(15),
                    end=_day(16),
                    place=14,
                    duration=2,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_the_largest_gap_of_the_day_is_not_a_lunch_break(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """`unknown`, not `unavailable`, and not 10:00.

        The collection succeeded and the honest answer is "no midday break
        today". `end_of_lessons` is the entity that says when this child comes
        home, and publishing the same instant twice would fire two
        automations for one homecoming.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        assert state.state == "unknown"
        assert "resumes_at" not in state.attributes


class TestWhenTheGapIsTooShortToBeLunch:
    """A changeover between two lessons is not a break."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """8-12, then 12:30-16: inside the window, but half an hour."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(12, 30),
                    end=_day(16),
                    place=9,
                    duration=7,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_thirty_minutes_at_noon_is_not_reported_as_a_break(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The window alone is not the rule; the duration is the other half.

        Without it, any timetable with a short changeover near noon would
        announce a lunch break the child never gets.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        assert state.state == "unknown"


class TestTheNextCancellation:
    """One filter for the state and the list, so they cannot disagree.

    The state is the start of the next cancelled lesson that is not over yet;
    `items` is every such lesson. Both use `end > now`, deliberately: two
    filters would drift, and a card would then show a list whose first entry
    is not the entity's own state.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Four slots: a cancellation already over, two to come, one exemption."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-OVER",
                    start=_day(7),
                    end=_day(7, 30),
                    place=0,
                    duration=1,
                    subject="Anglais",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-NEXT",
                    start=_day(10),
                    end=_day(11),
                    place=4,
                    duration=2,
                    subject="Mathématiques",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-LATER",
                    start=_day(14),
                    end=_day(15),
                    place=12,
                    duration=2,
                    subject="Histoire",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-EXEMPT",
                    start=_day(16),
                    end=_day(17),
                    place=16,
                    duration=2,
                    subject="Sport",
                    exempted=True,
                ),
            ]
        )
        return client

    async def test_the_state_is_the_start_of_the_next_one(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A timestamp, so an automation triggers without reading `items`."""
        del account
        state = hass.states.get("sensor.enfant_un_next_cancellation")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 10, 0, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"
        assert state.attributes["end"] == "2026-03-12T11:00:00+01:00"

    async def test_a_cancellation_already_over_is_in_neither_the_state_nor_the_list(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The single filter, checked on both outputs at once.

        The 07:00 slot was cancelled and has finished; reporting it would make
        the entity announce a cancellation nobody can still act on.
        """
        del account
        state = hass.states.get("sensor.enfant_un_next_cancellation")

        assert state is not None
        starts = [item["start"] for item in state.attributes["items"]]
        assert starts == [
            "2026-03-12T10:00:00+01:00",
            "2026-03-12T14:00:00+01:00",
        ]
        assert all("07:00" not in start for start in starts)

    async def test_an_exemption_is_not_reported_as_a_cancellation(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The lesson happens; this child is simply not required at it.

        Announcing it as cancelled would tell a parent the class was called
        off, which is a different fact with a different consequence.
        """
        del account
        state = hass.states.get("sensor.enfant_un_next_cancellation")

        assert state is not None
        subjects = [item["subject"] for item in state.attributes["items"]]
        assert "Sport" not in subjects


class TestWhenTheAfternoonWasCalledOff:
    """Why the child comes home early, without a template.

    `end_of_lessons` answers *when*, cancellations already removed. These two
    attributes answer *why*, and they are two because one would lie: an
    exemption moves the timetabled end just as a cancellation does, so
    `scheduled_end != state` is not "a class was called off". The test for
    that reading is `canceled_after > 0`.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """8-12 held, 14-15 cancelled, 15-17 cancelled."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(14),
                    end=_day(15),
                    place=12,
                    duration=2,
                    subject="Histoire",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-C",
                    start=_day(15),
                    end=_day(17),
                    place=14,
                    duration=4,
                    subject="Anglais",
                    canceled=True,
                ),
            ]
        )
        return client

    async def test_the_state_still_ignores_the_cancelled_lessons(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The attributes are added beside the state, not in place of it.

        An automation that triggers on the homecoming must keep working
        untouched; the new attributes are for the notification it sends.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 12, 0, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"

    async def test_the_timetabled_end_and_the_count_of_what_was_dropped(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A count, not a boolean: two cancelled hours is not one."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert state.attributes["scheduled_end"] == "2026-03-12T17:00:00+01:00"
        assert state.attributes["canceled_after"] == 2


class TestWhenTheChildIsMerelyExempted:
    """The case that makes `canceled_after` worth publishing separately.

    A day whose last slot is an exemption has a timetabled end later than its
    state and **nothing** cancelled. A consumer deriving "a class was called
    off" from `scheduled_end != state` would announce a cancellation here, on
    a day the class is being taught -- a claim a parent acts on once and never
    trusts again.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """8-12 held, then 14-16 taught but not required of this child."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(14),
                    end=_day(16),
                    place=12,
                    duration=4,
                    subject="Sport",
                    exempted=True,
                ),
            ]
        )
        return client

    async def test_the_timetabled_end_moves_but_nothing_was_cancelled(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """`scheduled_end` differs from the state and `canceled_after` is zero."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 12, 0, tzinfo=PARIS
        )
        assert state.attributes["scheduled_end"] == "2026-03-12T16:00:00+01:00"
        assert state.attributes["canceled_after"] == 0


class TestWhenTheWholeDayIsCancelled:
    """No lesson is attended, so the state is unknown -- and that is not enough.

    Every slot cancelled is exactly the day a parent must be told about, and
    the entity used to publish no attribute at all in that case, leaving the
    fact reachable only through a template over `lessons`. With no retained
    end, every cancellation counts as being after it.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Two slots, both cancelled."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(10),
                    place=0,
                    duration=4,
                    subject="Mathématiques",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(10),
                    end=_day(12),
                    place=4,
                    duration=4,
                    subject="Histoire",
                    canceled=True,
                ),
            ]
        )
        return client

    async def test_the_day_reports_its_timetable_and_counts_every_slot(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Unknown state, but the two attributes still carry the day."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert state.state == "unknown"
        assert state.attributes["scheduled_end"] == "2026-03-12T12:00:00+01:00"
        assert state.attributes["canceled_after"] == 2
        # No lesson closes the day, so there is nothing to name.
        assert "subject" not in state.attributes


class TestTheSubjectColourReachesTheStateMachine:
    """The colour PRONOTE gives a subject, published where a card can read it.

    This class exists because of a chain that was complete except for its last
    metre: the gateway decoded ``CouleurFond`` on four paths, three DTOs
    carried the field, and ``sensor.py`` mentioned it nowhere. Every dashboard
    therefore had to hand-write a colour table to show what PRONOTE already
    knew, and a card asking for the server's colour got nothing -- silently,
    which is the worst form.

    Three tiers and not one, because the field is spelled differently on each
    (``CouleurFond`` on a lesson and on a homework item, ``couleur`` on a
    subject average) and because upstream treats them differently: it resolves
    the homework one *strictly*, which is upstream saying the server always
    sends it there.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One coloured lesson and one uncoloured one, in that order."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(9),
                    place=0,
                    duration=2,
                    subject="Mathematiques",
                    background_color="#336699",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(9),
                    end=_day(10),
                    place=2,
                    duration=2,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_a_lesson_publishes_the_colour_the_server_sent(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The requirement, stated as a dashboard experiences it."""
        del account
        lessons = _attributes(hass, "sensor.enfant_un_lessons_today")["lessons"]

        assert lessons[0]["background_color"] == "#336699"

    async def test_a_lesson_without_a_colour_publishes_the_key_holding_none(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The key is always there; only its value can be absent.

        A key that appears and disappears makes every consumer write a
        membership test before a value test, and a template that forgets it
        renders the string ``None`` into a dashboard. Publishing ``None`` says
        "asked, and there is none", which is a different sentence from "this
        integration does not publish colours" -- and the two were confused for
        long enough to cost an afternoon.
        """
        del account
        lessons = _attributes(hass, "sensor.enfant_un_lessons_today")["lessons"]

        assert "background_color" in lessons[1]
        assert lessons[1]["background_color"] is None

    async def test_a_lesson_publishes_the_subject_identifier_beside_the_name(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A card mapping subjects to something of its own needs a stable key.

        The name is a bad one: PRONOTE writes it in capitals, with accents, and
        an establishment can rename it mid-year. The identifier was already on
        the DTO and published nowhere.
        """
        del account
        lessons = _attributes(hass, "sensor.enfant_un_lessons_today")["lessons"]

        assert lessons[0]["subject_id"] == "SUBJECT-MATHS"

    async def test_a_homework_item_publishes_its_colour(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The tier upstream resolves strictly, so the likeliest to carry one.

        The entity id is read from the translated NAME, not guessed from the
        translation key. This test asserted `sensor.enfant_un_homework_todo`
        first -- the key -- and there is no such entity: Home Assistant builds
        the suffix by slugifying the name, so `homework_todo` named "Homework
        to do" becomes `homework_to_do`. The same mistake had just been
        catalogued eleven times over in annexe A, which is what makes it worth
        a comment rather than a silent fix.
        """
        del account
        items = _attributes(hass, "sensor.enfant_un_homework_to_do")["items"]

        assert items
        assert items[0]["background_color"] == "#336699"

    async def test_a_subject_average_publishes_its_colour(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Spelled ``couleur`` upstream, absorbed by the gateway.

        `averages` is named "Subject averages", hence `subject_averages` and
        not `averages`. See the note on the homework test above.
        """
        del account
        items = _attributes(hass, "sensor.enfant_un_subject_averages")["items"]

        assert items
        assert items[0]["background_color"] == "#AA3366"


# ---------------------------------------------------------------------------
# The empty cases, which a card and an automation read together
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value_fn",
    [
        pytest.param("_history_grades", id="grades"),
        pytest.param("_history_averages", id="averages"),
        pytest.param("_history_overall", id="overall"),
        pytest.param("_history_report", id="report"),
        pytest.param("_history_absences", id="absences"),
        pytest.param("_history_delays", id="delays"),
        pytest.param("_history_punishments", id="punishments"),
        pytest.param("_history_evaluations", id="evaluations"),
    ],
)
def test_a_closed_period_the_history_holds_nothing_for_publishes_unknown(
    value_fn: str,
) -> None:
    """``None``, never ``0``, and this is the difference that matters.

    Eight per-period sensors share this shape, and every one of them counts
    something -- absences, delays, grades. A period the snapshot holds no
    record for is *unknown*, not zero: ``0`` is a perfectly valid
    ``numeric_state``, so an automation asking "fewer than one absence"
    would fire on a period that was never collected, which is the opposite of
    what it was written to mean.

    Parametrised over all eight because they are near-identical by design, and
    a ninth added tomorrow that returned ``0`` would be the easy mistake.
    """
    from custom_components.carnet_scolaire import sensor as sensor_module
    from custom_components.carnet_scolaire.models import HistoryFacts

    empty = HistoryFacts(marks=(), attendance=(), evaluations=())

    state, attributes = getattr(sensor_module, value_fn)(empty, 99)

    assert state is None
    assert attributes == {}


def test_a_closed_period_is_found_by_its_position_after_a_reconnection() -> None:
    """A history sensor keeps the period it was built with; the ``N`` moves.

    The entity is created at set-up and holds that login's ``Period``. The
    ``history`` tier later reads the closed periods of whatever session is
    open, and PRONOTE re-encrypts every period ``N`` at each login -- so a
    lookup on the ``N`` found nothing after the first reconnection and every
    closed-period sensor fell to ``unknown`` until the next reload.
    """
    from custom_components.carnet_scolaire.models import (
        AttendanceFacts,
        EvaluationsFacts,
        HistoryFacts,
    )
    from custom_components.carnet_scolaire.sensor import (
        _history_absences,
        _history_evaluations,
    )

    rotated = HistoryFacts(
        marks=(),
        attendance=(
            AttendanceFacts(
                period_id="PERIOD-0#session-2",
                period_index=1,
                absences=(),
                delays=(),
                punishments=(),
            ),
        ),
        evaluations=(
            EvaluationsFacts(
                period_id="PERIOD-0#session-2", period_index=1, evaluations=()
            ),
        ),
    )

    assert _history_absences(rotated, 1)[0] == 0
    assert _history_evaluations(rotated, 1)[0] == 0


def test_a_day_with_no_lesson_publishes_no_end_of_day_attributes() -> None:
    """An empty block, not a block of nulls.

    Home Assistant renders a missing attribute and an attribute holding
    ``None`` differently in a template, and "no lessons today" is every
    weekend and every holiday -- the commonest state this sensor has, not an
    edge case. The attributes also carry ``canceled_after``, and publishing a
    zero there on a day with no timetable at all would answer "nothing was
    cancelled today" about a day that was never a school day.
    """
    from custom_components.carnet_scolaire.sensor import _end_of_day_attributes

    from .test_delta import timetable

    class _Day:
        def today(self) -> object:
            return datetime(2026, 3, 15, tzinfo=PARIS).date()

        def now(self) -> datetime:
            return datetime(2026, 3, 15, 10, 0, tzinfo=PARIS)

    assert _end_of_day_attributes(timetable(), _Day()) == {}  # type: ignore[arg-type]


def test_a_period_with_no_grade_publishes_unknown_and_no_context() -> None:
    """The last grade is a number or nothing, never a word.

    ``14,5`` and ``Absent`` in the same state would be usable neither by a
    threshold nor by a graph (§4.3), so the sentinel lives in the attributes
    and the state stays numeric. With no grade at all there is nothing to put
    in either.
    """
    from custom_components.carnet_scolaire.sensor import (
        _latest_grade_attributes,
        _latest_grade_value,
    )

    from .test_delta import marks

    empty = marks()

    assert _latest_grade_value(empty, None) is None  # type: ignore[arg-type]
    assert _latest_grade_attributes(empty, None) == {}  # type: ignore[arg-type]


def test_a_period_with_no_report_card_publishes_no_report_attributes() -> None:
    """Most of the year there is no report card, and that is not missing data."""
    from custom_components.carnet_scolaire.sensor import _report_attributes

    from .test_delta import marks

    class _NoPeriods:
        state = type("S", (), {"periods": ()})()

    assert _report_attributes(marks(), _NoPeriods()) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Disabled by default (quality scale `entity-disabled-by-default`)
# ---------------------------------------------------------------------------

#: The translation keys whose entities are created disabled, and nothing else.
#: Written out rather than read off the code, so moving an entity in or out of
#: this set is a visible decision in a diff: disabling one that a blueprint, a
#: device automation or a dashboard card reads breaks it on every new install
#: without any error, since Home Assistant does not reject an entity id that
#: merely has no state.
DISABLED_BY_DEFAULT = frozenset(
    {
        ("sensor", "grades_period"),
        ("sensor", "averages_period"),
        ("sensor", "overall_average_period"),
        ("sensor", "report_card_period"),
        ("sensor", "absences_period"),
        ("sensor", "delays_period"),
        ("sensor", "punishments_period"),
        ("sensor", "evaluations_period"),
    }
)


async def test_only_the_closed_period_sensors_are_created_disabled(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The exact set, so the choice cannot drift in either direction.

    The closed-period sensors are eight per closed period, a number that grows
    through the year, for figures that no longer change and that nothing
    triggers on. Everything else stays enabled -- the limiter's diagnostics
    included, because the limiter card reads every one of them.
    """
    entries = er.async_entries_for_config_entry(
        er.async_get(hass), account.entry.entry_id
    )
    disabled = {
        (entry.domain, entry.translation_key)
        for entry in entries
        if entry.disabled_by is not None
    }
    assert disabled == DISABLED_BY_DEFAULT
    assert all(
        entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        for entry in entries
        if entry.disabled_by is not None
    )
    # Every closed-period entity, not merely one per key: two children, and
    # at least one closed period each.
    history = [
        entry
        for entry in entries
        if (entry.domain, entry.translation_key) in DISABLED_BY_DEFAULT
    ]
    assert len(history) > len(DISABLED_BY_DEFAULT)
    assert all(entry.disabled_by is not None for entry in history)
    assert all(hass.states.get(entry.entity_id) is None for entry in history)


async def test_the_closed_period_sensors_the_user_enables_publish_their_period(
    hass: HomeAssistant,
    account: PronoteAccount,
    account_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """Disabled by default must still mean *working* once enabled.

    Enabling is now the only way a household reaches these sensors, and it
    goes through a registry update followed by the reload Home Assistant
    schedules by itself -- exactly what the entity settings dialog does.
    """
    registry = er.async_get(hass)
    entry = account.entry
    # All of them, not one: each key has its own extractor, and a disabled
    # entity is never rendered, so an extractor nobody enables in a test is an
    # extractor nobody runs.
    targets = [
        candidate
        for candidate in er.async_entries_for_config_entry(registry, entry.entry_id)
        if (candidate.domain, candidate.translation_key) in DISABLED_BY_DEFAULT
    ]
    assert {candidate.translation_key for candidate in targets} == {
        key for _domain, key in DISABLED_BY_DEFAULT
    }

    with patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=account_client,
    ):
        for target in targets:
            registry.async_update_entity(target.entity_id, disabled_by=None)
        # `config_entries` reloads the entry thirty seconds after an
        # enablement (`RELOAD_AFTER_UPDATE_DELAY`).
        school_day.tick(timedelta(seconds=31))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        reloaded: PronoteAccount = entry.runtime_data
        assert reloaded is not account
        for _ in range(50):
            if reloaded.completed_ticks and not reloaded._tick_lock.locked():
                break
            await hass.async_block_till_done()

    for target in targets:
        state = hass.states.get(target.entity_id)
        assert state is not None, target.entity_id
        assert state.state != "unavailable", target.entity_id
        assert "period" in state.attributes
        assert "period_index" in state.attributes
