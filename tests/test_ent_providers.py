"""The ENT portal registry, and the Keycloak CAS login it adds.

Every page here is hand-written and every address fictional: the login is
exercised against a stand-in ``requests.Session``, never the network.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_components.carnet_scolaire.ent_providers import (
    LOCAL_PORTALS,
    PORTAL_TIMEOUT,
    ent_provider_names,
    keycloak_cas_login,
    resolve_ent_provider,
)

PRONOTE_URL = "https://demo.example.invalid/pronote/parent.html"
LOGIN_URL = "https://portal.example.invalid/auth/realms/demo/protocol/cas/login"

LOGIN_PAGE = """<html><body>
<form id="kc-form-login" onsubmit="return true;"
      action="/auth/realms/demo/login-actions/authenticate?session_code=abc&amp;tab_id=xyz"
      method="post">
  <input tabindex="1" id="username" name="username" value="" type="text">
  <input tabindex="2" id="password" name="password" type="password">
  <input type="hidden" id="id-hidden-input" name="credentialId" value="">
  <input type="checkbox" name="rememberMe">
  <input type="submit" value="Se connecter">
</form></body></html>"""

PRONOTE_PAGE = "<html><body><script>Start ({})</script></body></html>"


@dataclass
class _Answer:
    url: str
    text: str


@dataclass
class _FakeSession:
    """Records what the login sends; answers with the pages it was given."""

    get_page: _Answer
    post_page: _Answer
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=lambda: {"KC": "session"})
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _Answer:
        self.calls.append(("GET", url, kwargs))
        return self.get_page

    def post(self, url: str, **kwargs: Any) -> _Answer:
        self.calls.append(("POST", url, kwargs))
        return self.post_page


@dataclass
class _Portal:
    """The pages the stand-in portal serves, and every session opened on it."""

    pages: dict[str, _Answer]
    sessions: list[_FakeSession] = field(default_factory=list)


@pytest.fixture
def portal(monkeypatch: pytest.MonkeyPatch) -> _Portal:
    """Replace ``requests.Session`` with the stand-in portal."""
    import requests

    served = _Portal(
        pages={
            "get": _Answer(LOGIN_URL, LOGIN_PAGE),
            "post": _Answer(PRONOTE_URL, PRONOTE_PAGE),
        }
    )

    def factory() -> _FakeSession:
        session = _FakeSession(served.pages["get"], served.pages["post"])
        served.sessions.append(session)
        return session

    monkeypatch.setattr(requests, "Session", factory)
    return served


def test_the_hauts_de_seine_portal_is_offered_and_logs_in_through_cas() -> None:
    """The portal PRONOTE redirects to is offered, bound to the CAS login.

    `pronotepy` ships it commented out, and its `_oze_ent` binding dies before
    sending anything: the portal's home page is now a script-driven
    application without the login form. A parent reachable only through this
    portal had no login mode at all.
    """
    assert "enc_hauts_de_seine" in ent_provider_names()
    assert resolve_ent_provider("enc_hauts_de_seine") is keycloak_cas_login


def test_every_upstream_portal_is_still_offered_and_resolved_to_its_own_function() -> (
    None
):
    """Adding portals must not lose one: the upstream list is a subset, unchanged."""
    from pronotepy import ent as ent_module

    upstream = [
        name
        for name in dir(ent_module)
        if not name.startswith("_") and callable(getattr(ent_module, name))
    ]

    assert upstream
    assert set(upstream) <= set(ent_provider_names())
    for name in upstream:
        assert resolve_ent_provider(name) is getattr(ent_module, name)


def test_an_upstream_release_that_exports_a_portal_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `pronotepy` ships the name one day, its function wins over ours.

    Upstream is where a fix to that portal would land; a local copy that kept
    winning would pin the integration to whatever was true at the time.
    """
    from pronotepy import ent as ent_module

    def upstream_binding(username: str, password: str) -> None:
        """Stand-in for a binding a future release would export."""

    monkeypatch.setattr(
        ent_module, "enc_hauts_de_seine", upstream_binding, raising=False
    )

    assert resolve_ent_provider("enc_hauts_de_seine") is upstream_binding
    assert ent_provider_names().count("enc_hauts_de_seine") == 1


def test_an_unknown_name_resolves_to_nothing() -> None:
    """Callers turn `None` into a refusal; they must never receive a guess."""
    assert resolve_ent_provider("a_portal_nobody_ships") is None
    assert "a_portal_nobody_ships" not in ent_provider_names()
    assert set(LOCAL_PORTALS) <= set(ent_provider_names())


def test_the_login_follows_pronote_to_the_form_and_posts_the_credentials_there(
    portal: _Portal,
) -> None:
    """One GET from the PRONOTE address, one POST to the form's own action.

    The hidden fields travel with the credentials, the action's `&amp;` is
    decoded before use, and the relative action is joined to the page it came
    from -- the portal's, not PRONOTE's.
    """
    cookies = keycloak_cas_login(
        "parent-under-test", "not-a-real-password", pronote_url=PRONOTE_URL
    )

    (session,) = portal.sessions
    assert cookies == {"KC": "session"}
    (get, post) = session.calls
    assert get[:2] == ("GET", PRONOTE_URL)
    assert post[1] == (
        "https://portal.example.invalid/auth/realms/demo/login-actions/"
        "authenticate?session_code=abc&tab_id=xyz"
    )
    assert post[2]["data"] == {
        "username": "parent-under-test",
        "password": "not-a-real-password",
        "credentialId": "",
        "rememberMe": "",
    }
    assert get[2]["timeout"] == post[2]["timeout"] == PORTAL_TIMEOUT


def test_a_refused_password_is_an_ent_login_error(
    portal: _Portal,
) -> None:
    """The form coming back is Keycloak's refusal; the flow shows "invalid"."""
    from pronotepy.exceptions import ENTLoginError

    portal.pages["post"] = _Answer(LOGIN_URL, LOGIN_PAGE)

    with pytest.raises(ENTLoginError):
        keycloak_cas_login("parent-under-test", "wrong", pronote_url=PRONOTE_URL)


@pytest.mark.parametrize(
    "page",
    [
        pytest.param(PRONOTE_PAGE, id="no-form"),
        pytest.param(
            '<form id="kc-form-login"><input name="username"></form>', id="no-action"
        ),
    ],
)
def test_an_address_that_never_reaches_the_form_sends_no_credential(
    portal: _Portal, page: str
) -> None:
    """No form, no POST: the password is never sent anywhere else.

    Raised as `PronoteAPIError` so the flow reports the address, not the
    password -- telling a parent to retype a password that was never sent
    wastes a slot on the login guard.
    """
    from pronotepy.exceptions import ENTLoginError, PronoteAPIError

    portal.pages["get"] = _Answer(PRONOTE_URL, page)

    with pytest.raises(PronoteAPIError) as raised:
        keycloak_cas_login("parent-under-test", "secret", pronote_url=PRONOTE_URL)

    assert not isinstance(raised.value, ENTLoginError)
    (session,) = portal.sessions
    assert [call[0] for call in session.calls] == ["GET"]


def test_a_login_without_the_pronote_address_refuses_before_any_request(
    portal: _Portal,
) -> None:
    """The address is where the redirect starts; without it there is nothing to do."""
    from pronotepy.exceptions import PronoteAPIError

    with pytest.raises(PronoteAPIError):
        keycloak_cas_login("parent-under-test", "secret")

    assert portal.sessions == []
