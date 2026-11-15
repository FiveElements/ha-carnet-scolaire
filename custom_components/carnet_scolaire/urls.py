"""The PRONOTE address: normalised once, on the way in.

A PRONOTE address is a plain page URL --
``https://<host>/pronote/parent.html`` -- and ``pronotepy`` appends its own
query string. Nothing this integration does needs the query string or the
fragment of what the user pasted.

What the user pastes, on the other hand, is very often not that. Establishments
mail out deep links, ENT portals bounce back through single-sign-on with a
ticket in the URL, and a parent copying from the browser's address bar after
logging in copies whatever session parameter is sitting there. Keeping that
string meant a session parameter could travel into the config entry, from there
into a repair issue's placeholders (which are written to ``.storage`` and
rendered in the Repairs panel), and from there into the diagnostics download --
the file users are asked to attach to a public issue.

So the address is trimmed at the boundary, once, and the trimmed form is the
only one that is ever stored, displayed or reported. ``url_host`` narrows it
further for the cases that only need to say *which* establishment.
"""

from urllib.parse import urlsplit, urlunsplit


def public_url(raw: str | None) -> str:
    """Scheme, host and path -- no query string, no fragment, no credentials.

    Returns the input unchanged when it cannot be parsed as a URL, because
    refusing an address the user can see in their browser would be worse than
    storing an odd one: the login attempt will fail with the server's own
    message, which is a better diagnosis than ours.
    """
    if not raw:
        return ""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return raw.strip()
    if not parts.scheme or not parts.netloc:
        return raw.strip()
    # `netloc` rather than `hostname`, to keep a non-default port -- some
    # establishments publish one. Userinfo (`user:password@`) is dropped: it is
    # a credential, and PRONOTE never needs it.
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def url_host(raw: str | None) -> str:
    """Just the host, for diagnostics and for the entry's unique id."""
    if not raw:
        return ""
    try:
        host = urlsplit(raw.strip()).hostname
    except ValueError:
        return ""
    return host or ""
