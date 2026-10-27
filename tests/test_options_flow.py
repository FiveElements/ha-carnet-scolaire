"""The settings pages, which were the largest untested block in the flow.

Three sections and a live cost estimate. The estimate is the reason this file
matters more than a form test usually would: annexe B §7 requires the page to
show what the entered values will cost, computed by the *same* function that
produced the numbers in the annexe. A setting whose consequence you cannot see
gets set at random, and the consequence here is measured in requests against a
child's school account -- against a server whose one sanction applies to an IP
address.

Two failure modes drive the tests below, and neither is visible by reading the
code.

**A default outside its own field's range.** Every number field is built from
`const.OPTION_RANGES`, and every field also carries the documented default as
its `default=`. Nothing checks that the second is inside the first. The forms
are therefore submitted back *as displayed*: probatio validates the defaults
against the selectors that produced them, so a default that drifted out of
range fails here rather than on a user's screen.

**A section that resets the sections it does not show.** `_save` merges into the
stored options precisely because each page shows a subset. Replacing instead of
merging silently reverts every setting the user did not have on screen, which
is the kind of bug that is only noticed weeks later, as an unexplained change
in request volume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.data_entry_flow import FlowResultType
import probatio
import pytest

from custom_components.carnet_scolaire.config_flow import (
    PronoteOptionsFlow,
    _default,
    _entry_children,
    _measured_lifetime,
    _number,
)
from custom_components.carnet_scolaire.const import (
    CONF_CHILDREN,
    DEFAULT_MASTER_TICK,
    DEFAULT_TIER_INTERVALS,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_MASTER_TICK,
    OPT_MAX_REQUESTS_PER_DAY,
    OPT_QUIET_HOURS_ENABLED,
    OPT_TIER_ENABLED,
    OPT_TIER_INTERVAL,
    OPT_WRITE_OPERATIONS_ENABLED,
    OPTION_RANGES,
    Tier,
)
from custom_components.carnet_scolaire.options import estimate_daily_requests

from .conftest import CHILDREN, REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

SECTIONS = ("general", "tiers", "rate_limit")


#: The fields that are allowed to open empty, and why each one is.
#:
#: The rule this suspends is worth keeping for everything else: a field with no
#: value on screen is a field the page cannot be saved from without filling it
#: in, which reads as a broken integration. The timezone is the exception
#: because for that one field emptiness *is* a value -- "follow Home
#: Assistant" -- and a value has to be representable on screen to be
#: choosable. Naming it here rather than relaxing the assertion keeps the next
#: field that loses its default a failure.
_MAY_OPEN_EMPTY: frozenset[str] = frozenset({OPT_ESTABLISHMENT_TIMEZONE})


def _form_defaults(schema: probatio.Schema) -> dict[str, Any]:
    """The form exactly as it is displayed, ready to be submitted back.

    Submitting the displayed values is what makes these tests check the
    defaults rather than merely check that a form appears: every value goes
    back through the selector that produced it.

    "As displayed" is the whole contract, so a field prefilled by a
    `suggested_value` instead of a `default` is filled in here too -- Home
    Assistant sends back whatever is in the box, and a helper that dropped the
    suggestion would be modelling a user who deliberately cleared it. A field
    with neither is displayed empty and is omitted here, which is what the
    frontend does with an empty optional box.
    """
    filled: dict[str, Any] = {}
    for key in schema.schema:
        default = getattr(key, "default", None)
        if default is not None and default is not probatio.UNDEFINED:
            filled[str(key)] = default()
            continue
        suggested = (getattr(key, "description", None) or {}).get("suggested_value")
        if suggested is not None:
            filled[str(key)] = suggested
            continue
        assert str(key) in _MAY_OPEN_EMPTY, (
            f"{key} has neither a default nor a suggestion, so the page opens "
            "with an empty field. If that is deliberate, say why in "
            "_MAY_OPEN_EMPTY"
        )
    return filled


def _marker(schema: probatio.Schema) -> Any:
    """The timezone field's probatio marker, which carries its prefill.

    Read off the schema because the two ways a field can be prefilled --
    `default` and `description["suggested_value"]` -- are not
    interchangeable here, and which one is used is the fix.
    """
    return next(key for key in schema.schema if str(key) == OPT_ESTABLISHMENT_TIMEZONE)


async def _open(hass: HomeAssistant, entry: MockConfigEntry, section: str) -> Any:
    """Open the options flow and step through the menu to one section."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert list(result["menu_options"]) == list(SECTIONS)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": section}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == section
    return result


# ---------------------------------------------------------------------------
# Every field, submitted as displayed
# ---------------------------------------------------------------------------


@REQUIRES_HASS
@pytest.mark.parametrize("section", SECTIONS)
async def test_a_section_accepts_the_values_it_displays(
    hass: HomeAssistant, mock_entry: MockConfigEntry, section: str
) -> None:
    """Every default is inside the range of the field that shows it.

    The two tables are built independently -- `OPTION_RANGES` bounds the
    selector, `DEFAULT_*` fills it -- and nothing but this connects them. A
    default that drifted below its own minimum makes the page refuse to save
    without changing a single value, which reads as a broken integration
    rather than as a mistake in a constant.
    """
    form = await _open(hass, mock_entry, section)

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], _form_defaults(form["data_schema"])
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


@REQUIRES_HASS
async def test_every_tier_gets_an_interval_and_a_switch(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Ten tiers, twenty fields, and no tier quietly unreachable.

    A tier absent from this page cannot be turned off or slowed down by the
    person paying for its requests, and the omission would be invisible: the
    page would look complete.
    """
    form = await _open(hass, mock_entry, "tiers")
    fields = {str(key) for key in form["data_schema"].schema}

    for tier in DEFAULT_TIER_INTERVALS:
        assert OPT_TIER_INTERVAL.format(tier=tier.value) in fields
        assert OPT_TIER_ENABLED.format(tier=tier.value) in fields
    assert len(fields) == 2 * len(DEFAULT_TIER_INTERVALS)


@REQUIRES_HASS
async def test_a_tier_interval_is_bounded_away_from_zero(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Zero is the value that would make a tier due at every master tick.

    The ten interval keys are *formatted* -- `tier_interval_timetable` -- so
    they are not in `OPTION_RANGES`, and they were completely unbounded for a
    while. A stored `0` does not fail: it collects for ever, which is the
    single most expensive mistake this page can make.
    """
    form = await _open(hass, mock_entry, "tiers")
    schema = form["data_schema"]
    key = OPT_TIER_INTERVAL.format(tier=Tier.TIMETABLE.value)

    with pytest.raises(probatio.Invalid):
        schema({**_form_defaults(schema), key: 0})


# ---------------------------------------------------------------------------
# Merging, not replacing
# ---------------------------------------------------------------------------


@REQUIRES_HASS
async def test_saving_one_section_leaves_the_others_alone(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The whole reason `_save` merges.

    `mock_entry` opens with the suite's fast limiter options, none of which the
    "general" page displays. Replacing rather than merging would reset them to
    the defaults as a side effect of changing the master tick -- and the user
    would have no way of knowing which page did it.
    """
    before = dict(mock_entry.options)
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_MASTER_TICK: 7},
    )

    saved = result["data"]
    assert saved[OPT_MASTER_TICK] == 7
    for key, value in before.items():
        if key != OPT_MASTER_TICK:
            assert saved[key] == value, f"saving 'general' reset {key}"


@REQUIRES_HASS
async def test_the_write_switch_can_be_turned_on_and_stays_on(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The one option that changes what the integration is allowed to do.

    A boolean that failed to round-trip through the form would either leave
    writes permanently off -- silently ignoring the services -- or, worse, be
    read as on.
    """
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_WRITE_OPERATIONS_ENABLED: True},
    )

    assert result["data"][OPT_WRITE_OPERATIONS_ENABLED] is True


@REQUIRES_HASS
async def test_saving_the_page_does_not_turn_writes_on_for_an_old_entry(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """An entry without the option shows it off, so saving leaves it off.

    New entries are created with writes on, but the form is submitted as
    displayed: prefilled with that default, an entry from before the change
    would have had writes -- sending messages included -- switched on by
    whoever saved this page to change the tick (§8.3).
    """
    assert OPT_WRITE_OPERATIONS_ENABLED not in mock_entry.options
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], _form_defaults(form["data_schema"])
    )

    assert result["data"][OPT_WRITE_OPERATIONS_ENABLED] is False


@REQUIRES_HASS
async def test_quiet_hours_can_be_switched_off_from_the_rate_limit_page(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """A boolean whose stored value is `False` must not read as "unset".

    `options.get(key, True)` is the default, so a `False` that failed to
    persist would come back as `True` on the next page load -- quiet hours
    would appear to re-enable themselves overnight.
    """
    form = await _open(hass, mock_entry, "rate_limit")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_QUIET_HOURS_ENABLED: False},
    )

    assert result["data"][OPT_QUIET_HOURS_ENABLED] is False


# ---------------------------------------------------------------------------
# The timezone, the one field whose absence is a value
# ---------------------------------------------------------------------------
#
# Every other field on these three pages defaults to a constant, so storing
# that default explicitly is a no-op and nobody could tell. This one defaults
# to a *live reading of another system's mutable setting* -- Home Assistant's
# own timezone -- and §4.2 says so in as many words: the option's default "is
# Home Assistant's".
#
# That asymmetry is the whole defect. The page prefilled the box with
# `hass.config.time_zone` and Home Assistant submits a form back as displayed,
# so merely opening the "general" page to change the heartbeat converted a
# derived value into a pinned snapshot. Nothing said so, and there was no way
# back: the box could not be emptied, so once pinned, moving the instance to
# another timezone left every lesson time an hour out for ever.


def _unpin(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Take the timezone out of the entry's options.

    The suite's `FAST_OPTIONS` pins it, which is right for the collection tests
    -- they assert on wall-clock instants -- and wrong for these, whose whole
    subject is what happens when nothing is stored.
    """
    hass.config_entries.async_update_entry(
        entry,
        options={
            key: value
            for key, value in entry.options.items()
            if key != OPT_ESTABLISHMENT_TIMEZONE
        },
    )


@REQUIRES_HASS
async def test_visiting_the_general_page_does_not_pin_the_timezone(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The defect, stated as the invariant it broke.

    A user opening this page to change the heartbeat has said nothing about
    timezones, so nothing about timezones may be decided on their behalf. Once
    the value is stored here, `PronoteAccount` stops consulting
    `hass.config.time_zone` for good -- and the instance's timezone is a
    setting people really do change, on a move or on a first correct
    configuration.
    """
    _unpin(hass, mock_entry)
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {**_form_defaults(form["data_schema"]), OPT_MASTER_TICK: 7}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_MASTER_TICK] == 7
    assert OPT_ESTABLISHMENT_TIMEZONE not in result["data"], (
        "opening the page pinned the timezone as a side effect"
    )


@REQUIRES_HASS
async def test_the_box_opens_empty_when_the_timezone_is_not_pinned(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Empty is not an oversight, it is how "follow Home Assistant" is shown.

    Asserted on the schema rather than through a round trip because the two
    mechanisms differ in exactly the way that matters: a `default` is
    submitted back by the frontend, a `suggested_value` is too, and only the
    absence of both leaves a box a user can save without answering. Prefilling
    with the instance's zone -- even as a mere suggestion -- is what turned a
    visit into a decision.
    """
    _unpin(hass, mock_entry)
    form = await _open(hass, mock_entry, "general")

    marker = _marker(form["data_schema"])

    assert marker.default is probatio.UNDEFINED
    assert not (marker.description or {}).get("suggested_value")


@REQUIRES_HASS
async def test_a_pinned_timezone_is_shown_and_survives_an_untouched_save(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """A deliberate pin is a real setting and must not be lost by a visit.

    The symmetric risk to the defect above: making the field clearable would
    be worthless if the way to clear it were "open the page and save". A
    family following a school in another timezone has to be able to change the
    heartbeat without losing it.
    """
    form = await _open(hass, mock_entry, "general")

    marker = _marker(form["data_schema"])
    assert (marker.description or {})["suggested_value"] == "Europe/Paris"

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], _form_defaults(form["data_schema"])
    )

    assert result["data"][OPT_ESTABLISHMENT_TIMEZONE] == "Europe/Paris"


@REQUIRES_HASS
async def test_emptying_the_box_gives_the_instance_its_timezone_back(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The way back, which is the part a merge alone can never express.

    `_save` merges so that one section does not reset the others, and a merge
    can only ever add. With no rule for clearing, an option pinned once -- by
    the defect above, or by a user who has since moved -- could not be
    un-pinned from the interface at all.
    """
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_ESTABLISHMENT_TIMEZONE: ""},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert OPT_ESTABLISHMENT_TIMEZONE not in result["data"]


@REQUIRES_HASS
async def test_clearing_one_field_leaves_the_other_sections_alone(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Clearing is scoped to the fields the page actually showed.

    "Absent from the answers means cleared" is only true of the section being
    saved. Applied to the merge as a whole it would be catastrophic and
    silent: the "general" page shows none of the limiter tunables, so saving
    it would wipe every one of them -- which is the exact failure `_save` was
    written to merge away.
    """
    before = dict(mock_entry.options)
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_ESTABLISHMENT_TIMEZONE: ""},
    )

    saved = result["data"]
    for key, value in before.items():
        if key != OPT_ESTABLISHMENT_TIMEZONE:
            assert saved[key] == value, f"clearing the timezone also cleared {key}"


@REQUIRES_HASS
async def test_a_timezone_that_is_not_a_zone_is_refused_on_the_page(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Because the alternative is an account with no entities and no reason.

    The field is free text -- Home Assistant ships no timezone selector at
    this version -- and the string goes straight into `ZoneInfo`, inside
    `PronoteGateway.__init__`, on the set-up path. A `ZoneInfoNotFoundError`
    there fails the whole entry, so typing "Paris" instead of "Europe/Paris"
    took every entity of the account down and offered "unavailable" as the
    only explanation. Refusing it beside the box costs nothing and names the
    mistake.
    """
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_ESTABLISHMENT_TIMEZONE: "Paris"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_ESTABLISHMENT_TIMEZONE: "invalid_timezone"}
    assert mock_entry.options[OPT_ESTABLISHMENT_TIMEZONE] == "Europe/Paris", (
        "the refused value was stored anyway"
    )


@REQUIRES_HASS
async def test_a_refused_timezone_is_still_in_the_box_to_be_corrected(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Re-showing the form empty would ask the user to retype it from memory.

    Which matters more here than it sounds: the value is a long `Region/City`
    string, and what got it refused is usually one character in it.
    """
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            **_form_defaults(form["data_schema"]),
            OPT_ESTABLISHMENT_TIMEZONE: "Europe/Pariss",
        },
    )

    marker = _marker(result["data_schema"])

    assert (marker.description or {})["suggested_value"] == "Europe/Pariss"


@REQUIRES_HASS
async def test_a_timezone_pasted_with_stray_spaces_is_trimmed_not_refused(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """A copy-paste picks up whitespace, and `ZoneInfo` does not forgive it.

    Trimmed rather than refused, because " Europe/Paris" is not somebody
    saying something wrong -- it is somebody saying the right thing with an
    invisible character attached.
    """
    form = await _open(hass, mock_entry, "general")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            **_form_defaults(form["data_schema"]),
            OPT_ESTABLISHMENT_TIMEZONE: "  Europe/Brussels  ",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_ESTABLISHMENT_TIMEZONE] == "Europe/Brussels"


# ---------------------------------------------------------------------------
# The estimate, on every page
# ---------------------------------------------------------------------------


@REQUIRES_HASS
@pytest.mark.parametrize("section", SECTIONS)
async def test_the_estimate_is_on_every_page_and_agrees_with_the_annexe(
    hass: HomeAssistant, mock_entry: MockConfigEntry, section: str
) -> None:
    """Same function as the annexe's table, and shown wherever a value is set.

    Not decoration and not an approximation: a number computed by a second,
    "good enough" formula would disagree with the documentation, and the user
    would have no way of telling which one was wrong.
    """
    form = await _open(hass, mock_entry, section)
    placeholders = form["description_placeholders"]

    followed = len(mock_entry.data[CONF_CHILDREN])

    assert set(placeholders) == {"estimate", "students", "measured"}
    assert placeholders["students"] == str(followed)
    assert placeholders["estimate"] == str(
        estimate_daily_requests(
            mock_entry.options,
            students=followed,
            session_lifetime_minutes=None,
        )
    )


@REQUIRES_HASS
async def test_the_estimate_scales_with_the_children_actually_followed(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The number is per child, and it used to be untested at more than one.

    The entry fixture carried no ``children`` key, so every assertion about the
    estimate was made at ``students=1`` -- the one value at which the scaling
    cannot be observed. A parent following two children places nearly twice the
    traffic, and that is the figure the options page exists to show *before*
    somebody shortens an interval.

    Nearly twice, not exactly: the login and the session upkeep are shared, so
    the second child adds its tiers and not a second account's worth of
    overhead. Asserting a strict inequality on both sides holds that shape
    without hard-coding a number the annexe would have to be edited to match.
    """
    followed = len(mock_entry.data[CONF_CHILDREN])
    assert followed > 1, "the fixture must follow more than one child for this to test"

    form = await _open(hass, mock_entry, "general")
    for_many = int(form["description_placeholders"]["estimate"])
    for_one = estimate_daily_requests(
        mock_entry.options, students=1, session_lifetime_minutes=None
    )

    assert for_one < for_many < for_one * followed


@REQUIRES_HASS
async def test_the_menu_itself_carries_the_estimate(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The first screen is where a user decides whether to change anything.

    Putting the number only on the forms would mean it is first seen *after*
    the decision to go looking for it.
    """
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)

    assert result["description_placeholders"]["estimate"]


@REQUIRES_HASS
async def test_the_estimate_counts_the_children_the_entry_follows(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Two children cost more than one, and the page has to say so.

    A parent account shares a session and a login budget across children but
    not the per-child collections, so the estimate scales with the number
    followed -- which is exactly what a parent needs to see before adding the
    second one.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        data={
            **mock_entry.data,
            CONF_CHILDREN: [child_id for child_id, _ in CHILDREN],
        },
    )

    form = await _open(hass, mock_entry, "general")
    placeholders = form["description_placeholders"]

    assert placeholders["students"] == "2"
    assert placeholders["measured"] == "-"
    assert int(placeholders["estimate"]) > int(
        str(
            estimate_daily_requests(
                mock_entry.options, students=1, session_lifetime_minutes=None
            )
        )
    )


@REQUIRES_HASS
async def test_a_selection_stored_in_the_options_wins_over_the_entry_s(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The options are the newer answer, and the estimate follows them.

    A parent who narrows the selection to one child should see the cost drop
    on the page that let them do it, not after a reload.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        data={
            **mock_entry.data,
            CONF_CHILDREN: [child_id for child_id, _ in CHILDREN],
        },
        options={**mock_entry.options, CONF_CHILDREN: [CHILDREN[0][0]]},
    )

    form = await _open(hass, mock_entry, "general")

    assert form["description_placeholders"]["students"] == "1"


@REQUIRES_HASS
async def test_a_running_account_shows_the_lifetime_it_measured(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The estimate uses measurement when there is any, assumption otherwise.

    An estimate a user can be disappointed by is worse than no estimate
    (§6.5), which is why the pessimistic assumption -- not the optimistic one
    -- is what stands in before the first measurement.
    """
    form = await _open(hass, account.entry, "general")

    measured = form["description_placeholders"]["measured"]
    observed = account.extras.session.lifetime.observed_minutes

    if observed is None:
        assert measured == "-"
    else:
        assert measured == f"{observed:.0f}"


# ---------------------------------------------------------------------------
# The helpers behind the estimate, tested directly
# ---------------------------------------------------------------------------


class _Entry:
    """A config entry, as far as the estimate helpers read one."""

    def __init__(self, **attributes: Any) -> None:
        self.data: dict[str, Any] = {}
        self.__dict__.update(attributes)


class _Session:
    def __init__(self, lifetime: Any) -> None:
        self.lifetime = lifetime


class _Lifetime:
    def __init__(self, observed_minutes: float | None) -> None:
        self.observed_minutes = observed_minutes


def test_an_entry_that_is_not_loaded_has_measured_nothing() -> None:
    """`runtime_data` does not exist until the entry is set up.

    The options page is reachable on an entry whose setup failed -- it is one
    of the few things a user can still do there -- so this cannot raise.
    """
    assert _measured_lifetime(_Entry()) is None  # type: ignore[arg-type]


def test_a_loaded_account_with_no_sample_yet_still_measures_nothing() -> None:
    """Loaded is not the same as measured.

    `observed_minutes` is `None` until an expiry has actually been observed,
    and `None` must stay `None` rather than becoming `0.0` -- which the page
    would render as "the session dies instantly" and the estimator would treat
    as one login per call.
    """
    entry = _Entry(
        runtime_data=_Entry(extras=_Entry(session=_Session(_Lifetime(None))))
    )

    assert _measured_lifetime(entry) is None  # type: ignore[arg-type]


def test_a_measured_lifetime_comes_back_as_a_float() -> None:
    """Converted, not passed through: the estimator does arithmetic on it."""
    entry = _Entry(runtime_data=_Entry(extras=_Entry(session=_Session(_Lifetime(30)))))

    lifetime = _measured_lifetime(entry)  # type: ignore[arg-type]

    assert lifetime == 30.0
    assert isinstance(lifetime, float)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({}, 1, id="no selection recorded"),
        pytest.param({CONF_CHILDREN: []}, 1, id="an empty selection"),
        pytest.param({CONF_CHILDREN: None}, 1, id="a null selection"),
        pytest.param({CONF_CHILDREN: ["STUDENT-1"]}, 1, id="one child"),
        pytest.param({CONF_CHILDREN: ["STUDENT-1", "STUDENT-2"]}, 2, id="two children"),
    ],
)
def test_the_child_count_never_falls_below_one(
    data: dict[str, Any], expected: int
) -> None:
    """A student account follows no "children" and still costs requests.

    Zero would make the estimate zero, which is the one number that is certain
    to be wrong.
    """
    assert _entry_children(_Entry(data=data)) == expected  # type: ignore[arg-type]


def test_an_unset_option_falls_back_to_its_documented_default() -> None:
    """The default table in `const` is the single source, looked up by name.

    Resolved by name rather than duplicated here, so a default cannot be
    changed in the documentation and the annexe while the form keeps offering
    the old one.
    """
    assert _default({}, OPT_MASTER_TICK) == DEFAULT_MASTER_TICK
    assert _default({OPT_MASTER_TICK: 9}, OPT_MASTER_TICK) == 9


def test_every_number_field_is_bounded_by_the_table() -> None:
    """A field that accepts 100000 requests an hour will be set to 100000.

    Checked against `OPTION_RANGES` rather than against literals, which is the
    point: the same table bounds the field and the code that reads the stored
    value, so the two cannot drift apart.
    """
    low, high = OPTION_RANGES[OPT_MAX_REQUESTS_PER_DAY]
    selector = _number(OPT_MAX_REQUESTS_PER_DAY, "req/d")

    assert selector.config["min"] == float(low)
    assert selector.config["max"] == float(high)


def test_the_options_flow_is_the_one_the_config_flow_hands_out() -> None:
    """`async_get_options_flow` is a static callback with no entry state.

    It deliberately ignores the entry it is given -- the flow reads
    `self.config_entry`, which Home Assistant assigns -- so there is nothing
    here to get wrong except returning the wrong class, which is exactly what
    this checks.
    """
    from custom_components.carnet_scolaire.config_flow import PronoteConfigFlow

    assert isinstance(
        PronoteConfigFlow.async_get_options_flow(None),  # type: ignore[arg-type]
        PronoteOptionsFlow,
    )
