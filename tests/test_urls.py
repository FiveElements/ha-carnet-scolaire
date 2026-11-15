"""The address boundary: what a parent pasted, minus what must never be kept.

``urls.py`` is four branches of string handling, and that is exactly why it is
worth its own file. The string it trims is the one place an authentication
ticket enters this integration: establishments mail deep links, ENT portals
bounce back through single sign-on with ``?identifiant=...`` in the URL, and a
parent copying from the address bar after logging in copies whatever session
parameter is sitting there. Whatever survives ``public_url`` is stored in the
config entry, substituted into repair-issue placeholders -- written to
``.storage`` and rendered in the Repairs panel -- and written into the
diagnostics download, the file users are asked to attach to a public issue
(§8.2). A regression here is not a cosmetic one: it leaks a credential into a
file the user is instructed to publish.

The second promise is the opposite of the first, and just as load-bearing:
nothing here may *refuse* an address. A parent who can see a URL in their own
browser must be able to paste it; when it cannot be parsed the login fails with
the server's own message, which is a better diagnosis than anything this module
could invent. So every unparseable, scheme-less or plain-nonsense input has an
assertion below that it comes back unchanged rather than emptied or raised on.

Deliberately free of ``REQUIRES_HASS``: this is pure string code with no Home
Assistant import anywhere in its reach, so it must run on a contributor's
Windows checkout too -- where ``test_config_flow.py``, which owns the *flow*
half of the same promise, is skipped in its entirety.
"""

import pytest

from custom_components.carnet_scolaire.urls import public_url, url_host

#: A pasted deep link of the shape an ENT hands back: a page URL plus a
#: single-sign-on ticket. Synthetic, and saying so in the value itself.
PASTED_URL = (
    "https://demo.example.invalid/pronote/parent.html"
    "?login=true&identifiant=NOT-A-REAL-TICKET"
)
TRIMMED_URL = "https://demo.example.invalid/pronote/parent.html"


# ---------------------------------------------------------------------------
# public_url: the trimming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(TRIMMED_URL, TRIMMED_URL, id="already-bare"),
        pytest.param(PASTED_URL, TRIMMED_URL, id="sso-ticket"),
        pytest.param(f"{TRIMMED_URL}?", TRIMMED_URL, id="empty-query"),
        pytest.param(f"{TRIMMED_URL}#tab", TRIMMED_URL, id="fragment"),
        pytest.param(
            f"{PASTED_URL}#GenericAccueil", TRIMMED_URL, id="ticket-and-fragment"
        ),
    ],
)
def test_the_query_string_and_fragment_never_survive_the_boundary(
    raw: str, expected: str
) -> None:
    """The ticket must not reach ``.storage``, Repairs or the diagnostics.

    This is the whole reason the module exists. Keeping the query string meant a
    single-sign-on ticket travelled into the config entry, from there into a
    repair issue's placeholders, and from there into the download users are
    asked to attach to a public issue (§8.2) -- a credential published by
    someone following our own instructions.
    """
    assert public_url(raw) == expected


@pytest.mark.parametrize(
    "page",
    ["parent.html", "mobile.parent.html", "eleve.html", "professeur.html"],
    ids=["parent", "mobile-parent", "student", "teacher"],
)
def test_every_pronote_entry_page_keeps_its_own_path(page: str) -> None:
    """The path is trimmed of nothing: it selects the account space.

    ``pronotepy`` derives the account type from the last path segment, so
    rewriting ``mobile.parent.html`` to ``parent.html`` -- or dropping the
    segment as "noise" -- would log the parent in as the wrong kind of user, or
    not at all. Only the query string and the fragment are ours to remove.
    """
    raw = f"https://demo.example.invalid/pronote/{page}?identifiant=NOT-A-REAL-TICKET"
    assert public_url(raw) == f"https://demo.example.invalid/pronote/{page}"


def test_a_trailing_slash_is_left_exactly_as_pasted() -> None:
    """Path normalisation is not this function's job, and would break logins.

    A PRONOTE space answers on the directory form as well as on a page, and
    both are addresses a parent legitimately pastes. Silently adding or removing
    the slash would make the stored address differ from the one the user checked
    in their browser, which is the one thing they can compare against.
    """
    assert public_url("https://demo.example.invalid/pronote/") == (
        "https://demo.example.invalid/pronote/"
    )
    assert public_url("https://demo.example.invalid") == (
        "https://demo.example.invalid"
    )


def test_the_userinfo_is_dropped_because_it_is_a_credential() -> None:
    """``user:password@`` is a password, and PRONOTE never needs one in the URL.

    It arrives when a parent pastes from a password manager's stored link. Kept,
    it would be stored and reported exactly like the ticket above -- with the
    aggravation that a browser hides it from the address bar, so the user has no
    way of knowing what they handed over.
    """
    assert (
        public_url(
            "https://parent:not-a-real-password@demo.example.invalid/pronote/parent.html"
        )
        == TRIMMED_URL
    )


def test_a_non_default_port_is_kept() -> None:
    """Some establishments publish one, and dropping it makes the login fail.

    This is why the function rebuilds from ``netloc`` rather than ``hostname``:
    ``hostname`` would silently discard ``:8443`` and leave the parent with an
    address that cannot connect and an error that does not mention a port.
    """
    assert public_url("https://demo.example.invalid:8443/pronote/parent.html") == (
        "https://demo.example.invalid:8443/pronote/parent.html"
    )


def test_surrounding_whitespace_from_a_paste_is_absorbed() -> None:
    """A copy out of an e-mail carries a leading space or a trailing newline.

    Left in place it survives into the stored entry and into the unique id, so
    the same address pasted twice looks like two different accounts.
    """
    assert public_url(f"  {TRIMMED_URL}\n") == TRIMMED_URL


# ---------------------------------------------------------------------------
# public_url: what it refuses to refuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   \n", id="whitespace-only"),
    ],
)
def test_nothing_in_gives_the_empty_string_out(raw: str | None) -> None:
    """Callers concatenate the result, so ``None`` must not travel onwards.

    ``public_url`` is applied to values read back out of a config entry, where a
    key can be absent -- the diagnostics and the repair placeholders both do
    this. Returning ``None`` there would print the string "None" as the
    establishment's address in a bug report, or raise while building it.
    """
    assert public_url(raw) == ""


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not a url at all", id="prose"),
        pytest.param("demo.example.invalid/pronote/parent.html", id="no-scheme"),
        pytest.param(
            "//demo.example.invalid/pronote/parent.html", id="scheme-relative"
        ),
        pytest.param("mailto:someone@example.invalid", id="scheme-without-host"),
        pytest.param("https://[oops/pronote/parent.html", id="unparseable"),
    ],
)
def test_an_address_that_cannot_be_parsed_is_handed_back_untouched(raw: str) -> None:
    """Refusing it here would replace the server's diagnosis with a worse one.

    Each of these takes a different exit -- no scheme, no host, and a bracket
    that makes ``urlsplit`` itself raise ``ValueError`` -- and all three must
    behave the same way, because the alternative is a config flow that rejects
    an address while the parent is looking at it working in their browser. The
    login attempt fails with the establishment's own message instead, which
    actually tells them what is wrong.
    """
    assert public_url(raw) == raw.strip()


def test_the_scheme_is_never_invented_for_a_bare_host() -> None:
    """Guessing ``https://`` would turn a typo into a request somewhere else.

    A host with no scheme is passed through unchanged rather than promoted, so
    what is stored is what was pasted. Prepending a scheme here would mean the
    address in the entry is one this code made up, and the failure it produces
    points at the wrong thing.
    """
    assert not public_url("demo.example.invalid").startswith("http")


# ---------------------------------------------------------------------------
# url_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(PASTED_URL, "demo.example.invalid", id="sso-ticket"),
        pytest.param(TRIMMED_URL, "demo.example.invalid", id="bare"),
        pytest.param(
            "https://demo.example.invalid:8443/pronote/parent.html",
            "demo.example.invalid",
            id="port-dropped",
        ),
        pytest.param(
            "https://parent:not-a-real-password@demo.example.invalid/pronote/eleve.html",
            "demo.example.invalid",
            id="userinfo-dropped",
        ),
    ],
)
def test_the_host_is_all_that_is_left_for_the_places_that_only_need_it(
    raw: str, expected: str
) -> None:
    """Diagnostics say *which* establishment, and the path is more than that.

    Everything else -- port, credentials, path, ticket -- is stripped, because
    this value is written to the download attached to public issues. A helper
    that returned the whole address here would undo ``public_url``'s work in the
    one file where it matters most (§8.2).
    """
    assert url_host(raw) == expected


def test_the_host_is_lowercased_so_one_account_gets_one_unique_id() -> None:
    """The entry's unique id is built from this: two cases must not be two entries.

    Home Assistant deduplicates config entries by ``unique_id`` and by nothing
    else. If the case a parent happened to type survived, the same account added
    from a capitalised paste would be accepted as a second entry -- two
    schedulers, two budgets and a doubled request volume against a server whose
    one sanction applies to an IP address (§7.1).
    """
    assert url_host("HTTPS://Demo.Example.INVALID/pronote/parent.html") == (
        "demo.example.invalid"
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("nonsense", id="prose"),
        pytest.param("mailto:someone@example.invalid", id="scheme-without-host"),
        pytest.param("https://[oops/pronote/parent.html", id="unparseable"),
    ],
)
def test_an_address_with_no_host_yields_the_empty_string_rather_than_none(
    raw: str | None,
) -> None:
    """Unlike ``public_url``, this one has nothing safe to fall back to.

    There is no host to report, so it says so with a string. ``None`` would be
    worse than useless: the diagnostics writer and the unique-id builder both
    interpolate this value, so a ``None`` becomes the literal text "None" as an
    establishment name, or raises while assembling a bug report. The
    unparseable case is the one that has to be caught rather than allowed to
    propagate -- ``urlsplit`` raises ``ValueError`` on an unclosed bracket.
    """
    assert url_host(raw) == ""
