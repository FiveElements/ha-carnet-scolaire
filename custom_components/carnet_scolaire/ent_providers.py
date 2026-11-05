"""The ENT portals the login form offers, and how a stored name becomes a login.

A portal is persisted by **name**, because a config entry holds JSON and a
portal is a function. That name used to be resolved in three places -- the
form's option list, the flow's probe and the session's reconnection -- each
with its own ``getattr`` on ``pronotepy.ent``. They agreed only because
nothing had yet been added to one of them. This module is now the single
answer to both questions: which names exist, and what a name logs in with.

It also offers portals ``pronotepy`` does not ship working. Its ``ent.py``
carries ``enc_hauts_de_seine`` as a commented-out binding to ``_oze_ent``,
and binding it as written fails before any credential is sent: ``_oze_ent``
loads the portal's home page and looks for the Keycloak form there, but that
page is now a JavaScript application that redirects by script, so the form is
absent and the function dies on ``None.findAll``. Upstream disabled it in 2023
("Remove outdated CAS", #255) and has since declared the ENT functions
unmaintained (#335); a parent report from 2022 (#165) had already shown the
OZE fallback asking for the pupil's function where a parent needs ``TUT``.

What does work is the portal's **CAS** endpoint, and PRONOTE itself points at
it: an establishment reachable only through the portal answers every request
for ``parent.html`` -- ``?login=true`` included -- with a redirect to
``/auth/realms/<realm>/protocol/cas/login``, whose page carries a standard
Keycloak form. :func:`keycloak_cas_login` follows that redirect, submits the
form, and lets Keycloak send the browser back to PRONOTE with a ticket. It
needs nothing from the OZE API and nothing about the account's profile, which
is what made the upstream function wrong for parents.

Precedence goes to upstream: if a ``pronotepy`` release exports one of these
names, its own function is used and the local one is ignored.
"""

import html
import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin

if TYPE_CHECKING:
    from collections.abc import Callable

    import requests

#: Seconds allowed to each request of the portal login. The portal is not
#: PRONOTE and is not charged to the request budget, but an unbounded wait
#: would hold the single worker that every PRONOTE call of the entry shares.
PORTAL_TIMEOUT: Final = 30

_FORM: Final = re.compile(
    r'<form\b[^>]*\bid="kc-form-login"[^>]*>(.*?)</form>', re.DOTALL
)
_ACTION: Final = re.compile(r'<form\b[^>]*\baction="([^"]*)"')
_INPUT: Final = re.compile(r"<input\b[^>]*>")
_ATTRIBUTE: Final = re.compile(r'\b(name|value)="([^"]*)"')


def _login_form(page: str) -> tuple[str, dict[str, str]] | None:
    """The Keycloak login form's action and its pre-filled fields, if present."""
    form = _FORM.search(page)
    action = _ACTION.search(page[form.start() :]) if form else None
    if form is None or action is None:
        return None
    fields: dict[str, str] = {}
    for tag in _INPUT.findall(form.group(1)):
        attributes = dict(_ATTRIBUTE.findall(tag))
        if "name" in attributes:
            fields[attributes["name"]] = html.unescape(attributes.get("value", ""))
    return html.unescape(action.group(1)), fields


def keycloak_cas_login(
    username: str,
    password: str,
    pronote_url: str = "",
    **_opts: Any,
) -> requests.cookies.RequestsCookieJar:
    """Log in through the portal's Keycloak CAS page that PRONOTE redirects to.

    Signature and return value are ``pronotepy``'s ENT contract: it calls
    ``ent(username, password, pronote_url=...)`` and opens its PRONOTE
    session with the cookies returned. Neither credential is ever logged.

    The two failures are raised as the library's own, so the flow classifies
    them as it does every other portal: a refused password is an
    ``ENTLoginError`` ("invalid credentials"), and an address that never
    reaches the form is a ``PronoteAPIError`` ("no session page") -- the
    second is a wrong address or a closed space, and must not send a parent
    to retype a password that was never sent.
    """
    from pronotepy.exceptions import (  # noqa: PLC0415 -- optional dependency
        ENTLoginError,
        PronoteAPIError,
    )
    import requests  # noqa: PLC0415 -- optional dependency, as pronotepy's

    if not pronote_url:
        msg = "the portal login needs the PRONOTE address it redirects from"
        raise PronoteAPIError(msg)

    with requests.Session() as session:
        session.headers["User-Agent"] = "Mozilla/5.0"
        page = session.get(pronote_url, timeout=PORTAL_TIMEOUT)
        form = _login_form(page.text)
        if form is None:
            msg = "the PRONOTE address did not lead to the portal's login form"
            raise PronoteAPIError(msg)
        action, fields = form
        fields["username"] = username
        fields["password"] = password
        answer = session.post(
            urljoin(page.url, action), data=fields, timeout=PORTAL_TIMEOUT
        )
        if _login_form(answer.text) is not None:
            msg = "the portal refused the username or password"
            raise ENTLoginError(msg)
        return session.cookies


#: Portals offered in addition to ``pronotepy``'s own, by name.
LOCAL_PORTALS: Final[dict[str, Callable[..., Any]]] = {
    "enc_hauts_de_seine": keycloak_cas_login,
}


def _upstream() -> dict[str, Callable[..., Any]]:
    """The portals ``pronotepy.ent`` exports, by name."""
    from pronotepy import ent as ent_module  # noqa: PLC0415 -- optional dependency

    return {
        name: getattr(ent_module, name)
        for name in dir(ent_module)
        if not name.startswith("_") and callable(getattr(ent_module, name))
    }


def ent_provider_names() -> list[str]:
    """Every portal name the login form may offer, sorted."""
    return sorted(LOCAL_PORTALS.keys() | _upstream().keys())


def resolve_ent_provider(name: str) -> Callable[..., Any] | None:
    """The login function for ``name``, or ``None`` when no portal has it."""
    upstream = _upstream()
    if name in upstream:
        return upstream[name]
    return LOCAL_PORTALS.get(name)
