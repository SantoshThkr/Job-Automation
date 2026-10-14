"""Sign-in details for employer career sites, kept in the operating system's credential store.

Two kinds of password can let the assistant sign in on a company's career site for you:

* **a sign-in for one site** - stored in the macOS Keychain under ``personal-job-assistant:<site>`` with the
  account's own user name. It is used on that site (and its sub-domains) only, and only to *sign in* - never to
  create an account, never on another site, never on an identity provider's page (Google, Microsoft ...).
  Store one with ``python -m app.cli site-login set <site>``: the password is typed into macOS' own prompt and never
  passes through this program's arguments, its log, or the chat you are reading this in.
* **the general job-site password** ``PJA_ATS_ACCOUNT_PASSWORD`` in the git-ignored ``.env`` (as before): used with
  your profile e-mail where no sign-in is stored for the site, and for accounts the assistant creates.

Nothing here prints, logs or returns a password: values read from the Keychain are registered with the log filter
the moment they are read, and ``SiteCredential`` hides the password from ``repr``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)

SERVICE_PREFIX = "personal-job-assistant:"
_SECURITY = "/usr/bin/security"
# public suffixes with two labels that career sites commonly live under ("careers.acme.co.in" -> "acme.co.in")
_TWO_LABEL_SUFFIXES = {"co.in", "co.uk", "com.au", "co.nz", "com.sg", "co.za", "com.br", "co.jp", "net.in", "org.in",
                       "ac.in", "gov.in", "com.my", "co.id"}
_SITE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


@dataclass(frozen=True)
class SiteCredential:
    site: str  # what it is stored for ("careers.acme.com", or "*" for the general job-site password)
    username: str
    password: str = field(repr=False)
    source: str = "keychain"  # keychain | env

    @property
    def site_specific(self) -> bool:
        return self.source == "keychain"


def normalize_site(value: str) -> str:
    """'https://Careers.Acme.com/jobs/1' -> 'careers.acme.com' ('' when it is not a host name)."""
    v = (value or "").strip().lower()
    host = (urlparse(v).netloc if "://" in v else v.split("/")[0]).split("@")[-1].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host and _SITE.match(host) else ""


def site_keys(value: str) -> list[str]:
    """The names a sign-in for this address may be stored under, most specific first:
    'apply.careers.acme.com' -> ['apply.careers.acme.com', 'careers.acme.com', 'acme.com']. A public suffix
    ('co.in') is never one of them."""
    host = normalize_site(value)
    if not host:
        return []
    labels = host.split(".")
    if all(part.isdigit() for part in labels):  # an IP address has no parent domain
        return [host]
    keep = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return [".".join(labels[i:]) for i in range(0, max(len(labels) - keep, 0) + 1)]


def same_site(a: str, b: str) -> bool:
    """Both addresses belong to the same registrable domain (a login box framed from elsewhere is someone else's)."""
    ka, kb = site_keys(a), site_keys(b)
    return bool(ka and kb) and ka[-1] == kb[-1]


def keychain_available() -> bool:
    from app.config import get_settings

    return bool(get_settings().site_login_keychain and sys.platform == "darwin" and shutil.which(_SECURITY))


def _run(args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    """One call of macOS' ``security`` tool, output captured (it is never echoed anywhere)."""
    return subprocess.run([_SECURITY, *args], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603


def _keychain_entry(site: str) -> tuple[str, str] | None:
    """(user name, password) stored for exactly this site, or None."""
    service = SERVICE_PREFIX + site
    try:
        found = _run(["find-generic-password", "-s", service])
        if found.returncode != 0:
            return None
        secret = _run(["find-generic-password", "-s", service, "-w"])
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("keychain lookup failed: %s", exc.__class__.__name__)
        return None
    if secret.returncode != 0:
        return None
    password = secret.stdout.rstrip("\n")
    user = re.search(r'"acct"<blob>="((?:[^"\\]|\\.)*)"', found.stdout)
    return ((user.group(1) if user else ""), password) if password else None


def lookup(address: str, *, default_username: str = "") -> SiteCredential | None:
    """The sign-in to use on ``address`` (a URL or host): the one stored for that site or a parent domain of it,
    else the general job-site password with your profile e-mail, else None. Never returns anything for an address
    that is not a web host."""
    from app.config import get_settings
    from app.security import register_secret

    keys = site_keys(address)
    if not keys:
        return None
    if keychain_available():
        for key in keys:
            entry = _keychain_entry(key)
            if entry is not None:
                register_secret(entry[1])
                return SiteCredential(key, entry[0] or default_username, entry[1], "keychain")
    general = get_settings().ats_account_password
    if general:
        return SiteCredential("*", default_username, general, "env")
    return None


def store(site: str, username: str) -> int:
    """Store (or replace) the sign-in for ``site``. macOS asks for the password itself, twice, in the terminal:
    this program never sees it. Returns the tool's exit status."""
    key = normalize_site(site)
    if not key:
        raise ValueError(f"'{site}' is not a site name (expected something like careers.example.com)")
    if not username.strip():
        raise ValueError("a user name (usually your e-mail address on that site) is required")
    if not (sys.platform == "darwin" and shutil.which(_SECURITY)):
        raise RuntimeError("the macOS Keychain is not available here: use PJA_ATS_ACCOUNT_PASSWORD in .env instead")
    # "-w" last and without a value: `security` prompts for the password on the terminal (nothing in the process list)
    return subprocess.run([_SECURITY, "add-generic-password", "-U", "-s", SERVICE_PREFIX + key, "-a", username.strip(),  # noqa: S603
                           "-l", f"Job Assistant sign-in: {key}", "-w"], check=False).returncode


def delete(site: str) -> bool:
    key = normalize_site(site)
    if not key or not (sys.platform == "darwin" and shutil.which(_SECURITY)):
        return False
    return _run(["delete-generic-password", "-s", SERVICE_PREFIX + key]).returncode == 0


def stored_sites() -> list[tuple[str, str]]:
    """(site, user name) of every stored sign-in. Reads item names only - no password is requested."""
    if not (sys.platform == "darwin" and shutil.which(_SECURITY)):
        return []
    try:
        dump = _run(["dump-keychain"], timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out: dict[str, str] = {}
    for block in dump.split("keychain: ")[1:]:
        service = re.search(r'"svce"<blob>="' + re.escape(SERVICE_PREFIX) + r'([^"]+)"', block)
        if service:
            user = re.search(r'"acct"<blob>="((?:[^"\\]|\\.)*)"', block)
            out[service.group(1)] = user.group(1) if user else ""
    return sorted(out.items())
