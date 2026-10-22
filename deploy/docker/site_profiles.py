"""Opt-in per-site crawl profiles (GrantGunner fork).

A caller may pass ``"site_profile": "youtube"`` on POST /crawl or
/crawl/stream. Only then does the server add a small, fixed set of browser
settings that the untrusted request body itself is never allowed to set
(cookies and headers stay forbidden for callers). With no ``site_profile``
nothing in this module runs, so the default path is unchanged.

The ``youtube`` profile:
  * is restricted to youtube.com hosts (every URL in the request must match,
    otherwise the request is rejected with 400);
  * sets the SOCS and CONSENT cookies so YouTube serves the real page instead
    of the EU consent interstitial (consent.youtube.com);
  * sends an English Accept-Language header so labels such as durations and
    "3 days ago" are stable for parsers.

No JavaScript or hooks are injected: the channel banner and the first page
of uploads (with durations) are in the server-rendered ytInitialData once the
consent wall is gone.
"""

from typing import Iterable, List, Optional
from urllib.parse import urlsplit

# "Reject all" consent record (SOCS) plus the legacy CONSENT cookie.
YOUTUBE_SOCS = "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLC_pwY"
YOUTUBE_CONSENT = "YES+cb"

YOUTUBE_COOKIES = [
    {"name": "SOCS", "value": YOUTUBE_SOCS, "domain": ".youtube.com", "path": "/", "secure": True},
    {"name": "CONSENT", "value": YOUTUBE_CONSENT, "domain": ".youtube.com", "path": "/"},
]
YOUTUBE_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}

SITE_PROFILES = {"youtube"}


class SiteProfileError(ValueError):
    """Unknown profile or a URL outside the profile's hosts (maps to 400)."""


def is_youtube_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return host == "youtube.com" or host.endswith(".youtube.com")


def validate_site_profile(profile: Optional[str], urls: Iterable[str]) -> Optional[str]:
    """Return the normalised profile name, or None when no profile was asked for."""
    if profile is None:
        return None
    name = str(profile).strip().lower()
    if name not in SITE_PROFILES:
        raise SiteProfileError(f"Unknown site_profile {profile!r}; supported: {sorted(SITE_PROFILES)}")
    bad: List[str] = [u for u in urls if not is_youtube_host(u)]
    if bad:
        raise SiteProfileError(f"site_profile 'youtube' only accepts youtube.com URLs; rejected: {bad[:3]}")
    return name


def apply_site_profile(profile: Optional[str], browser_config) -> None:
    """Mutate an already-validated BrowserConfig for the given profile.

    Called after the untrusted body has been loaded, so these server-chosen
    values bypass the body allowlist on purpose. No-op when profile is None.
    """
    if not profile:
        return
    if profile == "youtube":
        existing = [c for c in (browser_config.cookies or []) if c.get("name") not in {"SOCS", "CONSENT"}]
        browser_config.cookies = existing + [dict(c) for c in YOUTUBE_COOKIES]
        browser_config.headers = {**(browser_config.headers or {}), **YOUTUBE_HEADERS}
