"""The profile photo, whose cost must not scale with dashboard renders.

``async_image`` was the largest untested surface in the package: nineteen of
``image.py``'s statements, including every branch that decides whether a
request is placed. That matters more than its size suggests, because this is
the one entity Home Assistant asks for again on *every* render of a card --
so a caching mistake here does not cost one request, it costs one per viewer
per refresh, against a daily cap of a few thousand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from custom_components.carnet_scolaire.const import Tier
from custom_components.carnet_scolaire.image import PronoteProfileImage
from custom_components.carnet_scolaire.ratelimit import TierDeferred

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS


def _photo_entity(hass: HomeAssistant, account: PronoteAccount) -> PronoteProfileImage:
    """The photo entity of the first child, built as the platform builds it."""
    return PronoteProfileImage(
        hass,
        account,
        account.coordinators[Tier.STATIC],
        account.students[0],
    )


async def _attachment(account: PronoteAccount, parent_client: FakeClient) -> Any:
    """The fake attachment the first child's photo resolves to."""
    student_id = account.students[0].id
    return next(
        info.profile_picture
        for info in parent_client.children
        if str(info.id) == student_id
    )


async def test_the_photo_is_fetched_once_and_then_served_from_memory(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """A card re-rendered ten times must cost one request, not ten.

    ``FakeAttachment.data`` counts its reads for exactly this assertion: the
    second call must come back with the same bytes without touching the
    attachment again.
    """
    entity = _photo_entity(hass, account)
    attachment = await _attachment(account, parent_client)
    before = attachment.reads

    first = await entity.async_image()
    second = await entity.async_image()

    assert first == b"not-a-real-image"
    assert second == first
    assert attachment.reads == before + 1


async def test_an_establishment_that_publishes_no_photo_is_asked_only_once(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """``None`` is an answer, and it must be remembered like any other.

    Retrying on every render would spend the daily budget on a picture that
    does not exist -- and a school with no photos is an ordinary
    configuration, not an error.
    """
    for info in parent_client.children:
        info._photo = None

    entity = _photo_entity(hass, account)

    assert await entity.async_image() is None
    assert await entity.async_image() is None


async def test_a_deferred_photo_may_be_asked_again_later(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """Postponed is not "no photo", and conflating the two is permanent.

    ``TierDeferred`` means the limiter refused a low-priority call because the
    budget was tight. Caching that as an answer would leave the entity empty
    until the next reload, for a picture that was there all along.

    The limiter's own deferral logic is exercised exhaustively in
    ``test_ratelimit.py`` -- held at 100 % -- so the refusal is injected at the
    session seam here rather than by draining a budget of twenty thousand.
    """
    entity = _photo_entity(hass, account)
    extras = account.extras
    assert extras is not None

    with patch.object(extras.session, "run", side_effect=TierDeferred("budget", 60.0)):
        assert await entity.async_image() is None

    # No longer marked as attempted, so the real fetch still happens.
    assert await entity.async_image() == b"not-a-real-image"


async def test_a_failure_to_fetch_the_photo_does_not_break_the_card(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """A missing face must not take a dashboard down with it.

    The bare ``except`` is deliberate and is the reason this test exists: it
    swallows anything the photo path can raise, so the assertion has to be
    that the entity survives rather than that a particular exception is
    handled.
    """
    entity = _photo_entity(hass, account)
    extras = account.extras
    assert extras is not None

    with patch.object(extras.session, "run", side_effect=RuntimeError("upstream")):
        assert await entity.async_image() is None

    # Attempted, and therefore not retried: the failure is remembered.
    with patch.object(extras.session, "run", side_effect=AssertionError("re-asked")):
        assert await entity.async_image() is None


async def test_the_photo_entity_is_available_before_its_first_fetch(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """Its availability answers "does the child have a photo", not "is it loaded".

    The base class would answer from the snapshot, which makes the entity
    unavailable until the static tier has run -- and an `unavailable` image
    entity renders as a broken card rather than as an empty one.
    """
    assert _photo_entity(hass, account).available is True


async def test_a_static_tier_with_no_coordinator_creates_no_photo_entity(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """Capability and coordinator are two separate questions.

    The source can announce the static tier and still have no coordinator for
    it -- disabled in the options, most simply. Reading the capability alone
    would hand the entity a ``None`` coordinator, and the ``AttributeError``
    lands inside the platform forward, which takes the whole entry down rather
    than one photo.
    """
    from custom_components.carnet_scolaire.image import async_setup_entry

    built: list[object] = []

    def collect(entities: object, *_args: object, **_kwargs: object) -> None:
        built.extend(entities)  # type: ignore[arg-type]

    removed = account.coordinators.pop(Tier.STATIC)
    try:
        await async_setup_entry(hass, mock_entry, collect)  # type: ignore[arg-type]
    finally:
        account.coordinators[Tier.STATIC] = removed

    assert built == []
