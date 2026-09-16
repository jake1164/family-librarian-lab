"""Real CWA and Audiobookshelf helpers for the lab's cwa-local/abs profiles.

MailpitClient is re-exported from se-lab (agent.simulators.smtp), not
redefined here -- it was already 100% Mailpit-protocol-only with zero
Family Librarian knowledge, so it moved to se-lab as a reusable fixture
client rather than staying a copy only this lab could import. Import it
from here (clients.MailpitClient, unchanged for every existing call site)
or directly from agent.simulators.smtp -- both are the same class.

Not se-lab ClientPlugins: se-lab's generic `clients up/down/reset` commands
(agent/commands/clients.py) all route through agent.common.compose_up()/
compose_command(), which requires docker-config/docker-compose.yaml and a
COMPOSE_PROFILES env var -- the same runtime-compose-file mechanism this lab
deliberately does not use (see commands.py's own `_compose()`, kept separate
for per-case Compose project isolation since m3undle-lab-public's session).
Forcing CWA/ABS onto that generic path would hit the same class of
Compose-profile bug already found and fixed once this session (`clients
down` missing containers outside the active profile scope) -- so these are
plain helper classes driven directly from suite setup/case code instead,
covering exactly what a suite needs: bring the destination to a known-ready
state, and independently verify a published item actually landed in it.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent.simulators import matrix as matrix_fixture
from agent.simulators import smtp as smtp_fixture
from agent.simulators.matrix import MatrixTestClient  # noqa: F401 -- re-exported for suite use
from agent.simulators.smtp import MailpitClient  # noqa: F401 -- re-exported, see module docstring

CWA_SERVICE = "cwa"
CWA_PROFILE = "cwa-local"
CWA_INTERNAL_URL = "http://cwa:8083"
CWA_DEFAULT_IMAGE = "crocodilestick/calibre-web-automated:latest"
CWA_DEFAULT_HOST_PORT = 18083
CWA_DEFAULT_USERNAME = "admin"
# CWA ships this account by default with this exact fixed password baked into
# the image itself -- unrelated to any real deployment's credentials, and
# every scenario run is a fresh, disposable CWA instance. Not ours to change:
# confirmed for real that editing this constant alone does NOT change the
# account CWA actually created (a plain OPDS request with the new value
# 401s), it just breaks every check/login in this file that relies on it.
CWA_DEFAULT_PASSWORD = "admin123"
CWA_INGEST_CONTAINER_PATH = "/cwa-ingest"

# CWA's own outbound relay for Kindle/e-reader delivery -- see compose.base.yaml's
# cwa-mailpit service comment for why this is a second, separate Mailpit
# instance rather than the `smtp` profile's STARTTLS-only one. Folded into the
# `cwa-local` profile itself (not a separate opt-in profile) so a plain
# `./lab up --profile cwa-local` always has working Kindle delivery with no
# extra flags.
CWA_MAILPIT_SERVICE = "cwa-mailpit"
CWA_MAILPIT_INTERNAL_HOST = "cwa-mailpit"
CWA_MAILPIT_INTERNAL_PORT = 1025
CWA_MAILPIT_DEFAULT_HOST_PORT = 18029

# The dedicated CWA account Family Librarian signs in as to invoke CWA's own
# "send to e-reader" route (see FamilyLibrarian.Domain.Publishing.CwaSettings.
# EreaderServiceAccountUsername) -- fixed test-only credentials, unrelated to
# any real deployment's, created fresh in every disposable CWA instance.
# `allow_additional_ereader_emails` is granted because Family Librarian always
# passes an explicit per-request recipient override (`selected_emails` on
# CWA's own /send_selected route) rather than relying on this account's own
# registered kindle_mail -- confirmed against CWA's real `/admin/user/new`
# form, which exposes that exact checkbox alongside a per-account kindle_mail
# field FL's flow never uses (family-librarian's own kindle-delivery-beta-plan
# doc, "Family Librarian users should not need corresponding CWA user accounts
# solely for Kindle delivery" -- this is CWA's single shared service account,
# not one per FL user).
CWA_EREADER_SERVICE_ACCOUNT_USERNAME = "fl-ereader-service"
# CWA enforces password complexity server-side on account creation (min 8,
# upper, lower, digit, special) -- confirmed for real: a plain lowercase
# fixed-value password like every other lab credential in this file gets
# silently rejected ("Password doesn't comply with password validation
# rules"), leaving no account behind and every later sign-in failing.
CWA_EREADER_SERVICE_ACCOUNT_PASSWORD = "Admin123!"

# Two extra seeded member accounts (beyond the bootstrap admin) so Kindle
# delivery has real per-user DeliveryTargets to exercise -- the admin
# deliberately gets none (matches how a real household's admin account
# usually isn't itself a reader with a Kindle). Fixed test-only credentials,
# same "no lab.env edits required" convention as the bootstrap admin.
CWA_READER1_EMAIL = "reader1@sydneyelvis.net"
CWA_READER2_EMAIL = "reader2@sydneyelvis.net"
CWA_READER_DEFAULT_PASSWORD = "Admin123!"
# Real Kindle "Send to Kindle" addresses are personal, Amazon-account-linked
# secrets -- never hardcoded. FAMILY_LIBRARIAN_READER1_KINDLE_EMAIL/
# _READER2_KINDLE_EMAIL in lab.env (gitignored) override these; left unset,
# every reader's Kindle target still gets configured, just pointed at a fake
# address that only ever reaches cwa-mailpit above, never a real device.
CWA_READER1_KINDLE_FALLBACK = "reader1@kindle-lab.test"
CWA_READER2_KINDLE_FALLBACK = "reader2@kindle-lab.test"

# The SFTP sidecar is the only writer exposed to Family Librarian in these two
# profiles -- CWA itself sees the same backing `cwa-ingest` volume it already
# uses for cwa-local (mounted into the sftp container at the chrooted upload
# subdir), so a file that arrives via SFTP becomes visible in CWA's OPDS
# catalog exactly the way CWA-L-02 already proves for a local-filesystem
# write. Two services, not one profile-conditional one: atmoz/sftp's user
# credentials are baked into its startup command, which Compose has no way to
# vary per active profile within a single service definition.
CWA_SFTP_SERVICE_KEY = "sftp-key"
CWA_SFTP_SERVICE_PASSWORD = "sftp-password"
CWA_SFTP_PROFILE_KEY = "cwa-sftp-key"
CWA_SFTP_PROFILE_PASSWORD = "cwa-sftp-password"
CWA_SFTP_DEFAULT_IMAGE = "atmoz/sftp:alpine"
CWA_SFTP_USERNAME = "cwaftp"
CWA_SFTP_INGEST_PATH = "/upload"
CWA_SFTP_PORT = 22
CWA_SFTP_DEFAULT_PASSWORD = "Admin123!"
# Matches CWA's own PUID/PGID above -- same "Access is denied" class of bug
# already found and fixed for the local-ingest profile; the sftp sidecar and
# CWA both write/read the same shared volume and must agree on ownership.
CWA_SFTP_UID_GID = 1654

ABS_SERVICE = "abs"
ABS_PROFILE = "abs"
ABS_INTERNAL_URL = "http://abs:80"
ABS_DEFAULT_IMAGE = "advplyr/audiobookshelf:latest"
ABS_DEFAULT_HOST_PORT = 18378
ABS_DEFAULT_USERNAME = "test-admin"
ABS_DEFAULT_PASSWORD = "Admin123!"
ABS_LIBRARY_NAME = "lab-audiobooks"
ABS_LIBRARY_FOLDER_PATH = "/audiobooks"

# HTTPS-only, deliberately not a plain static-file server: GutenbergCatalogOptions/
# GutenbergMirrorOptions validate every configured URL as absolute HTTPS at DI
# startup (confirmed against real code, no bypass hook exists) -- see
# ensure_gutenberg_fixture_tls() in commands.py for the self-signed CA this
# depends on. Same-project service, resolved by name like cwa/abs; unlike
# ClamAV it does not need suite-wide sharing (cheap to start, no meaningful
# per-case cost), so it's brought up per-case via `extra_profiles` same as
# CWA/ABS/SFTP.
GUTENBERG_PROFILE = "gutenberg"
GUTENBERG_SERVICE = "gutenberg-fixture"
GUTENBERG_FIXTURE_INTERNAL_URL = "https://gutenberg-fixture"

# Real, disposable SMTP catcher (Mailpit) for the smtp suite -- proves
# MailKitSmtpTestSender's actual connect/STARTTLS/authenticate/send path,
# which family-librarian's own test suite never exercises (it force-registers
# AlwaysSucceedsSmtpTestSender for every in-repo test). TLS is not optional:
# SmtpSecurityMode has no plaintext option, so even the happy path negotiates
# real STARTTLS against the cert ensure_smtp_fixture_tls() issues
# (commands.py) -- the internal host below is also that cert's CN.
SMTP_PROFILE = "smtp"
SMTP_SERVICE = "mailpit"
SMTP_INTERNAL_HOST = "mailpit"
SMTP_INTERNAL_PORT = 1025
# Nothing listens here inside the mailpit container -- a real, deterministic
# ECONNREFUSED for SMTP-04, no dependency on external/wildcard DNS behavior.
SMTP_UNREACHABLE_PORT = 19999
SMTP_DEFAULT_IMAGE = "axllent/mailpit:latest"
# Mailpit's own HTTP API/UI -- published so the lab's Python client can
# independently verify delivery, the same "assert against the real
# destination's own API" pattern AbsClient/CwaClient already follow.
SMTP_DEFAULT_HOST_PORT = 18025
# Must match docker/mailpit/smtp-auth-file's bcrypt entry -- Mailpit rejects
# any other SMTP AUTH credentials, the only way to get a real,
# deterministic AuthenticationException out of MailKitSmtpTestSender
# (SMTP-03) rather than faking one. Sourced from se-lab's MailpitFixture
# (agent.simulators.smtp) rather than redeclared here: that module's
# DEFAULT_SMTP_AUTH_FILE_CONTENT is the actual bcrypt hash
# docker/mailpit/smtp-auth-file was generated from, so this stays the one
# place both could drift from, not two.
SMTP_AUTH_USERNAME = smtp_fixture.DEFAULT_SMTP_AUTH_USERNAME
SMTP_AUTH_PASSWORD = smtp_fixture.DEFAULT_SMTP_AUTH_PASSWORD

# Real, disposable Matrix homeserver (Continuwuity) for the matrix suite --
# proves HttpMatrixClient/MatrixOutboundCommunicationProvider/
# MatrixInboundSyncHostedService's actual register/create-room/send/sync
# path, which family-librarian's own test suite never exercises
# (IMatrixClient is mocked in every in-repo test). See compose.base.yaml's
# own `matrix` service comment for why there's no Docker healthcheck, and
# se-lab's agent.simulators.matrix module for the live-verified detail
# behind the image choice and the bootstrap-registration-token behavior.
MATRIX_PROFILE = "matrix"
MATRIX_SERVICE = "matrix"
MATRIX_INTERNAL_HOST = "matrix"
MATRIX_INTERNAL_PORT = 8008
MATRIX_SERVER_NAME = "matrix.example.test"
MATRIX_DEFAULT_IMAGE = "ghcr.io/continuwuity/continuwuity:latest"
# Matrix's own Client-Server API -- published so the lab's Python client can
# register test accounts and drive the two-way (COMM-1 §D) flow directly.
MATRIX_DEFAULT_HOST_PORT = 18008
# Must match compose.base.yaml's own `matrix` service
# CONTINUWUITY_REGISTRATION_TOKEN. Sourced from se-lab's
# MatrixHomeserverFixture the same way SMTP_AUTH_USERNAME/_PASSWORD are
# sourced from MailpitFixture above -- one place either could drift from.
MATRIX_REGISTRATION_TOKEN = matrix_fixture.DEFAULT_REGISTRATION_TOKEN
# Fixed test accounts -- registered fresh against every scenario's own new
# homeserver instance (nothing persists across scenarios), same "no
# lab.env edits required" convention as every other fixed credential here.
# Password is the one standard test password used everywhere else in this
# file (ABS/CWA e-reader/readers) -- the sole fixed exception is
# CWA_DEFAULT_PASSWORD, which is baked into CWA itself, not lab-chosen.
MATRIX_BOT_USERNAME = "fl-bot"
MATRIX_BOT_PASSWORD = "Admin123!"
# The Matrix-protocol identity (a separate account space, registered
# straight against Continuwuity) for the same person CWA_READER1_EMAIL
# already names on the Family Librarian side -- matching usernames so the
# lab has exactly one "reader1" persona instead of a second, differently
# named one that only exists for Matrix. FL's own reader account for
# linking is CWA_READER1_EMAIL/CWA_READER_DEFAULT_PASSWORD directly
# (ensure_reader() is idempotent, so reusing it here is safe whether or
# not the cwa-local profile already created it).
MATRIX_HOUSEHOLD_USERNAME = "reader1"
MATRIX_HOUSEHOLD_PASSWORD = "Admin123!"


def wait_for_matrix_ready(base_url: str, *, timeout_seconds: float = 60.0) -> None:
    """Thin AssertionError-raising wrapper around se-lab's own
    matrix_fixture.wait_for_ready() -- that check is protocol-only (does
    GET /_matrix/client/versions answer 200?), not tied to how the
    homeserver was launched, so it belongs there rather than duplicated
    here. `docker compose up --wait` only proves the container reached
    "running" -- there's no Docker healthcheck for this image to wait on
    (see compose.base.yaml's own comment) -- not that the process inside
    has finished binding its listener."""
    if not matrix_fixture.wait_for_ready(base_url, timeout=timeout_seconds):
        raise AssertionError(f"Matrix homeserver never became ready at {base_url} within {timeout_seconds}s.")


_BOOK_ID_PATTERN = re.compile(r"/opds/(?:book|download)/(\d+)")


def _http(
    url: str,
    *,
    method: str = "GET",
    json_body: object | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    basic_auth: tuple[str, str] | None = None,
    bearer_token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, bytes]:
    request_headers = dict(headers or {})
    if json_body is not None:
        if data is not None:
            raise ValueError("Specify either json_body or data, not both.")
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if basic_auth is not None:
        raw = f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        request_headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    if bearer_token is not None:
        request_headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # local Compose URL supplied by the lab
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError:
        return 0, b""


@dataclass(slots=True)
class CwaClient:
    """Drives real Calibre-Web-Automated over its default OPDS/basic-auth
    surface -- no bootstrap needed, the image ships a working admin account."""

    host_base_url: str
    username: str = CWA_DEFAULT_USERNAME
    password: str = CWA_DEFAULT_PASSWORD

    def ready(self) -> bool:
        status, _ = _http(f"{self.host_base_url}/opds", basic_auth=(self.username, self.password))
        return status == 200

    def find_book(self, title: str, author: str | None) -> str | None:
        """Mirrors FamilyLibrarian.Infrastructure.Publishing.CwaCatalogClient's
        own OPDS search + acquisition-link parsing -- an independent check
        that the destination's own catalog shows the item, not just that
        Family Librarian's own status says so."""
        matches = self.find_books(title, author)
        return matches[0] if matches else None

    def find_books(self, title: str, author: str | None) -> list[str]:
        """Return every matching OPDS book id for duplicate assertions.

        A local ingest write is not proof that CWA imported the file.  The
        restart/recheck scenarios also need to prove that verifying an
        existing handoff did not create a second catalog item, so preserve all
        matching entries instead of collapsing the feed to its first result.
        """
        import urllib.parse

        url = f"{self.host_base_url}/opds/search/{urllib.parse.quote(title)}"
        status, body = _http(url, basic_auth=(self.username, self.password))
        if status != 200:
            return []
        return _parse_matching_book_ids(body.decode("utf-8", errors="replace"), title, author)

    def find_books_with_language(self, title: str, author: str | None) -> list[tuple[str, str | None]]:
        """Same matching as `find_books`, but also returns each entry's
        declared language (or `None` if the feed carries no language element
        under either Dublin Core namespace CWA could plausibly use).

        Exists to verify what CWA's *real* OPDS feed emits independently of
        Family Librarian's own `CwaCatalogClient` -- which only reads
        `dcterms:language` (http://purl.org/dc/terms/). If a scenario finds a
        language here under the *other* common Dublin Core namespace
        (http://purl.org/dc/elements/1.1/, `dc:language`) instead, that is a
        real product gap (CwaCatalogClient parsing the wrong namespace), not
        a lab-fixture problem -- keeping the two namespaces distinguishable
        in the return value is what makes that diagnosable.
        """
        import urllib.parse

        url = f"{self.host_base_url}/opds/search/{urllib.parse.quote(title)}"
        status, body = _http(url, basic_auth=(self.username, self.password))
        if status != 200:
            return []
        return _parse_matching_books_with_language(body.decode("utf-8", errors="replace"), title, author)


_CSRF_INPUT_PATTERN = re.compile(r"""name=["']csrf_token["'][^>]*value=["']([^"']*)["']""", re.IGNORECASE)


@dataclass(slots=True)
class CwaAdminSession:
    """Drives CWA's real admin web UI -- a stateful, CSRF-protected
    Flask-Login session, not a JSON API (CWA exposes no admin REST surface;
    this mirrors FamilyLibrarian.Infrastructure.Publishing.CwaEreaderSessionClient's
    own login flow, verified against the same real image: GET /login to scrape
    a CSRF token and pick up the anonymous session cookie, POST /login, then a
    fresh CSRF token is available on the now-authenticated page). Used only to
    provision the fixture CWA instance itself (mail settings, the e-reader
    service account) -- everything Family Librarian's own delivery *from* CWA
    goes through is exercised via CwaEreaderSessionClient in the app under
    test, never duplicated here.

    Unlike the C# client, this relies on urllib's default redirect handling
    rather than manually inspecting the 302: a cookie-processing opener
    already follows POST /login's redirect to `/` and returns that page's
    body directly, so a fresh CSRF token is scraped from wherever redirects
    land. A failed login re-renders /login (200, with a password field) rather
    than redirecting, which is what distinguishes failure from success here.
    """

    host_base_url: str
    username: str = CWA_DEFAULT_USERNAME
    password: str = CWA_DEFAULT_PASSWORD
    _opener: Any = None
    _csrf_token: str | None = None

    def __post_init__(self) -> None:
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _get(self, path: str) -> tuple[int, str]:
        request = urllib.request.Request(f"{self.host_base_url}{path}", method="GET")
        with self._opener.open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", errors="replace")

    def _post_form(self, path: str, fields: dict[str, str]) -> tuple[int, str]:
        data = urllib.parse.urlencode(fields).encode("ascii")
        request = urllib.request.Request(f"{self.host_base_url}{path}", data=data, method="POST")
        with self._opener.open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", errors="replace")

    def sign_in(self) -> bool:
        """Idempotent -- safe to call before every admin action since no
        session is cached across CwaAdminSession instances/runs either."""
        _, login_page = self._get("/login")
        pre_login_token = _CSRF_INPUT_PATTERN.search(login_page)
        if pre_login_token is None:
            return False

        status, body = self._post_form("/login", {
            "username": self.username,
            "password": self.password,
            "csrf_token": pre_login_token.group(1),
        })
        if status != 200 or 'name="password"' in body:
            return False

        post_login_token = _CSRF_INPUT_PATTERN.search(body)
        if post_login_token is None:
            return False
        self._csrf_token = post_login_token.group(1)
        return True

    def configure_mail_settings(
        self, *, smtp_host: str, smtp_port: int, login: str, password: str, from_address: str,
        encryption: str = "None",
    ) -> None:
        """POSTs CWA's real /admin/mailsettings form (field names verified
        against the actual image: mail_server_type=0 is CWA's "Standard Email
        Account" mode). `encryption` is one of "None"/"StartTls"/"SslOnConnect"
        (CWA's own mail_use_ssl 0/1/2) -- "None" is what cwa-mailpit's
        MP_SMTP_AUTH_ALLOW_INSECURE=true relay needs (confirmed for real: this
        exact payload against it produced an authenticated, delivered test
        email); a real external relay (Gmail and similar) needs "StartTls" or
        "SslOnConnect" instead -- unlike cwa-mailpit's self-signed cert, a real
        relay's certificate is already trusted by CWA's own default OS trust
        store, so no lab-side cert plumbing is needed for that case."""
        encryption_values = {"None": "0", "StartTls": "1", "SslOnConnect": "2"}
        if encryption not in encryption_values:
            raise ValueError(f"encryption must be one of {sorted(encryption_values)}, got {encryption!r}.")

        if not self.sign_in() or self._csrf_token is None:
            raise AssertionError("CWA admin sign-in failed while configuring mail settings.")

        status, _ = self._post_form("/admin/mailsettings", {
            "csrf_token": self._csrf_token,
            "mail_server_type": "0",
            "mail_server": smtp_host,
            "mail_port": str(smtp_port),
            "mail_use_ssl": encryption_values[encryption],
            "mail_login": login,
            "mail_password_e": password,
            "mail_from": from_address,
            "mail_size": "25",
            "submit": "submit",
        })
        if status != 200:
            raise AssertionError(f"CWA /admin/mailsettings POST returned HTTP {status}.")

    def ensure_ereader_service_account(self, *, username: str, password: str, email: str) -> None:
        """POSTs CWA's real /admin/user/new form. Idempotent by construction
        rather than by checking first: this always posts the same fixed
        username/password, so a CWA instance that already has the account
        (a reused volume across manual `up`/`down` cycles) simply gets a
        harmless "already exists" rejection from CWA, leaving the account
        exactly as an earlier successful run left it -- a fresh disposable
        instance (every automated scenario) gets a real create instead.
        Grants only download_role/viewer_role plus
        allow_additional_ereader_emails (see the module-level constant's
        comment) -- deliberately not admin_role/upload_role/edit_role/etc.,
        since this account only ever needs to sign in and hit
        /send_selected/<book_id>."""
        if not self.sign_in() or self._csrf_token is None:
            raise AssertionError("CWA admin sign-in failed while creating the e-reader service account.")

        status, body = self._post_form("/admin/user/new", {
            "csrf_token": self._csrf_token,
            "name": username,
            "email": email,
            "password": password,
            "kindle_mail": "",
            "kindle_mail_subject": "",
            "allow_additional_ereader_emails": "on",
            "locale": "en",
            "default_language": "all",
            "theme": "1",
            "download_role": "on",
            "viewer_role": "on",
        })
        if status != 200:
            raise AssertionError(f"CWA /admin/user/new POST returned HTTP {status}.")

        # CWA always answers 200 here, success or not -- confirmed for real:
        # a fresh create flashes flash_success, a validation failure (e.g. the
        # password complexity rule this account's fixed password must satisfy)
        # flashes flash_danger with status 200 too, which a bare status check
        # would silently accept as "done" while leaving no usable account
        # behind. "Found an existing account" is this method's own expected
        # idempotent no-op (see docstring); any other flash_danger is real.
        if 'id="flash_danger"' in body and "existing account" not in body:
            danger = re.search(r'id="flash_danger"[^>]*>([^<]*)<', body)
            message = danger.group(1) if danger else "unknown error"
            raise AssertionError(f"CWA /admin/user/new rejected the e-reader service account: {message}")


def _parse_first_matching_book_id(atom_xml: str, title: str, author: str | None) -> str | None:
    matches = _parse_matching_book_ids(atom_xml, title, author)
    return matches[0] if matches else None


def _parse_matching_book_ids(atom_xml: str, title: str, author: str | None) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(atom_xml)
    except ET.ParseError:
        return []

    matches: list[str] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns):
        entry_title = entry.findtext("atom:title", default="", namespaces=ns)
        if title.lower() not in entry_title.lower():
            continue
        if author:
            author_element = entry.find("atom:author/atom:name", ns)
            entry_author = author_element.text if author_element is not None else None
            if entry_author and author.lower() not in entry_author.lower():
                continue
        for link in entry.findall("atom:link", ns):
            href = link.get("href")
            match = _BOOK_ID_PATTERN.search(href) if href else None
            if match:
                matches.append(match.group(1))
                break
    return matches


# The two Dublin Core namespaces a real OPDS feed could plausibly use for a
# language element -- CwaCatalogClient (Family Librarian's own parser) only
# reads DCTERMS_LANGUAGE_TAG; DC_ELEMENTS_LANGUAGE_TAG exists here purely so a
# scenario can tell "CWA emits no language at all" apart from "CWA emits it
# under the other namespace, which the product doesn't read" -- two very
# different findings that would otherwise look identical (language: None).
DCTERMS_LANGUAGE_TAG = "{http://purl.org/dc/terms/}language"
DC_ELEMENTS_LANGUAGE_TAG = "{http://purl.org/dc/elements/1.1/}language"


def _parse_matching_books_with_language(
    atom_xml: str, title: str, author: str | None
) -> list[tuple[str, str | None]]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(atom_xml)
    except ET.ParseError:
        return []

    matches: list[tuple[str, str | None]] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns):
        entry_title = entry.findtext("atom:title", default="", namespaces=ns)
        if title.lower() not in entry_title.lower():
            continue
        if author:
            author_element = entry.find("atom:author/atom:name", ns)
            entry_author = author_element.text if author_element is not None else None
            if entry_author and author.lower() not in entry_author.lower():
                continue
        book_id: str | None = None
        for link in entry.findall("atom:link", ns):
            href = link.get("href")
            match = _BOOK_ID_PATTERN.search(href) if href else None
            if match:
                book_id = match.group(1)
                break
        if book_id is None:
            continue
        language = entry.findtext(DCTERMS_LANGUAGE_TAG) or entry.findtext(DC_ELEMENTS_LANGUAGE_TAG)
        matches.append((book_id, language))
    return matches


def ensure_sftp_test_keypair(directory: Path) -> tuple[str, Path]:
    """Idempotently generates a disposable ed25519 keypair used only to
    authenticate the lab's own probe/upload to the throwaway cwa-sftp-key
    sidecar -- never a real deployment's credential, and safe to leave on
    disk across runs since every SFTP server it talks to is itself a fresh,
    disposable container. Returns (private_key_pem_text, public_key_directory)
    -- the directory is what compose.base.yaml bind-mounts at
    /home/cwaftp/.ssh/keys, atmoz/sftp's documented "any *.pub file here is an
    authorized key" location."""
    directory.mkdir(parents=True, exist_ok=True)
    private_path = directory / "id_ed25519"
    public_path = directory / "id_ed25519.pub"
    if not private_path.exists():
        subprocess.run(
            [
                "ssh-keygen", "-t", "ed25519", "-N", "", "-C", "family-librarian-lab-sftp-test",
                "-f", str(private_path),
            ],
            check=True,
            capture_output=True,
        )
        public_path.chmod(0o644)
    return private_path.read_text(encoding="utf-8"), directory


@dataclass(slots=True)
class AbsClient:
    """Drives real Audiobookshelf: first-run root-user init, login, and
    library/folder setup, then the library-item lookup FL's own
    AudiobookshelfApiClient uses -- an independent check that the
    destination's own API shows the item, not just that Family Librarian's
    own status says so."""

    host_base_url: str
    username: str = ABS_DEFAULT_USERNAME
    password: str = ABS_DEFAULT_PASSWORD
    _token: str | None = None
    _library_id: str | None = None
    _folder_id: str | None = None

    def ready(self) -> bool:
        status, _ = _http(f"{self.host_base_url}/healthcheck")
        return status == 200

    def ensure_bootstrapped(self) -> tuple[str, str, str]:
        """Idempotently init the root user (skips if already initialized),
        log in, and ensure the lab's audiobook library/folder exist. Returns
        (api_token, library_id, folder_id)."""
        if self._token is None:
            self._token = self._ensure_root_user_and_login()
        if self._library_id is None or self._folder_id is None:
            self._library_id, self._folder_id = self._ensure_library()
        return self._token, self._library_id, self._folder_id

    def _ensure_root_user_and_login(self) -> str:
        status, body = _http(f"{self.host_base_url}/status")
        if status != 200:
            raise AssertionError(f"Audiobookshelf /status returned HTTP {status}.")
        is_init = json.loads(body).get("isInit", False)
        if not is_init:
            init_status, init_body = _http(
                f"{self.host_base_url}/init",
                method="POST",
                json_body={"newRoot": {"username": self.username, "password": self.password}},
            )
            if init_status != 200:
                raise AssertionError(f"Audiobookshelf /init returned HTTP {init_status}: {init_body!r}")
        login_status, login_body = _http(
            f"{self.host_base_url}/login",
            method="POST",
            json_body={"username": self.username, "password": self.password},
        )
        if login_status != 200:
            raise AssertionError(f"Audiobookshelf /login returned HTTP {login_status}: {login_body!r}")
        token = json.loads(login_body).get("user", {}).get("token")
        if not isinstance(token, str) or not token:
            raise AssertionError("Audiobookshelf login response did not contain a user token.")
        return token

    def _ensure_library(self) -> tuple[str, str]:
        assert self._token is not None
        status, body = _http(f"{self.host_base_url}/api/libraries", bearer_token=self._token)
        if status != 200:
            raise AssertionError(f"Audiobookshelf GET /api/libraries returned HTTP {status}.")
        for library in json.loads(body).get("libraries", []):
            if library.get("name") == ABS_LIBRARY_NAME:
                folders = library.get("folders", [])
                if folders:
                    return library["id"], folders[0]["id"]

        create_status, create_body = _http(
            f"{self.host_base_url}/api/libraries",
            method="POST",
            json_body={
                "name": ABS_LIBRARY_NAME,
                "folders": [{"fullPath": ABS_LIBRARY_FOLDER_PATH}],
                "mediaType": "book",
                "provider": "audible",
            },
            bearer_token=self._token,
        )
        if create_status != 200:
            raise AssertionError(f"Audiobookshelf POST /api/libraries returned HTTP {create_status}: {create_body!r}")
        created = json.loads(create_body)
        return created["id"], created["folders"][0]["id"]

    def trigger_scan(self) -> None:
        """Explicitly request a library scan rather than waiting on
        Audiobookshelf's own upload-triggered scan, which was found not to
        fire reliably for an upload coming from Family Librarian's own HTTP
        client (confirmed for real: the identical file uploaded via curl
        triggers an immediate scan; the same file via Family Librarian's
        publishing pipeline does not, and Docker Desktop's fallback polling
        watcher -- "inotify unavailable" -- doesn't pick it up in any bounded
        test deadline either). Idempotent -- a scan with nothing new to find
        is a normal, cheap no-op."""
        assert self._token is not None and self._library_id is not None
        _http(f"{self.host_base_url}/api/libraries/{self._library_id}/scan", method="POST", bearer_token=self._token)

    def find_item(
        self, title: str, author: str | None, *, timeout_seconds: float = 30.0, rescan: bool = False
    ) -> str | None:
        """Polls the library-item list -- Audiobookshelf only recognizes a
        known audio extension on scan, so this also confirms the upload
        actually scanned in, not merely landed on disk. `rescan` re-triggers
        a scan on every poll, for callers driving Audiobookshelf's own
        eventual-consistency directly rather than waiting on Family
        Librarian's separate verification loop to notice."""
        assert self._token is not None and self._library_id is not None
        deadline = time.monotonic() + timeout_seconds
        while True:
            if rescan:
                self.trigger_scan()
            status, body = _http(
                f"{self.host_base_url}/api/libraries/{self._library_id}/items", bearer_token=self._token
            )
            if status == 200:
                match = _find_matching_item_id(json.loads(body), title, author)
                if match is not None:
                    return match
            if time.monotonic() >= deadline:
                return None
            time.sleep(1.5)

    def find_items(self, title: str, author: str | None) -> list[str]:
        """Return every matching library item so idempotency tests can prove
        that a publish reuses an ABS item instead of adding another one."""
        assert self._token is not None and self._library_id is not None
        status, body = _http(
            f"{self.host_base_url}/api/libraries/{self._library_id}/items", bearer_token=self._token
        )
        if status != 200:
            return []
        return _find_matching_item_ids(json.loads(body), title, author)

    def audio_track_filenames(self, item_id: str) -> list[str]:
        """Read Audiobookshelf's own item representation and return its track order.

        A delivery status only proves Family Librarian believes it handed an
        asset off.  This verifies the destination retained one bundle's
        individual files in order after its real scan.
        """
        assert self._token is not None
        status, body = _http(f"{self.host_base_url}/api/items/{item_id}", bearer_token=self._token)
        if status != 200:
            raise AssertionError(f"Audiobookshelf GET /api/items/{item_id} returned HTTP {status}: {body!r}")
        response = json.loads(body)
        item = response.get("libraryItem", response) if isinstance(response, dict) else response
        media = item.get("media") if isinstance(item, dict) else None
        audio_files = media.get("audioFiles") if isinstance(media, dict) else None
        if not isinstance(audio_files, list):
            raise AssertionError(f"Audiobookshelf item has no audioFiles list: {response!r}")

        filenames: list[str] = []
        for audio_file in audio_files:
            metadata = audio_file.get("metadata") if isinstance(audio_file, dict) else None
            filename = metadata.get("filename") if isinstance(metadata, dict) else None
            if not isinstance(filename, str) or not filename:
                raise AssertionError(f"Audiobookshelf audio file has no metadata filename: {audio_file!r}")
            filenames.append(filename)
        return filenames

    def seed_item(self, content: bytes, filename: str, title: str, author: str) -> str:
        """Seed one item through ABS's supported upload API, not its database
        or managed-library filesystem. This intentionally mirrors Family
        Librarian's real multipart contract for idempotency scenarios."""
        token, library_id, folder_id = self.ensure_bootstrapped()
        boundary = f"----family-librarian-lab-abs-{time.monotonic_ns()}"
        fields = (
            ("library", library_id),
            ("folder", folder_id),
            ("title", title),
            ("author", author),
        )
        payload = bytearray()
        for name, value in fields:
            payload.extend(f"--{boundary}\r\n".encode("ascii"))
            payload.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
            payload.extend(value.encode("utf-8"))
            payload.extend(b"\r\n")
        payload.extend(f"--{boundary}\r\n".encode("ascii"))
        payload.extend(
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode("ascii")
        )
        payload.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        payload.extend(content)
        payload.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
        status, body = _http(
            f"{self.host_base_url}/api/upload",
            method="POST",
            data=bytes(payload),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            bearer_token=token,
            timeout=60,
        )
        if status < 200 or status >= 300:
            raise AssertionError(f"Audiobookshelf seed upload returned HTTP {status}: {body!r}")
        item_id = self.find_item(title, author, timeout_seconds=45, rescan=True)
        if item_id is None:
            raise AssertionError("Audiobookshelf did not expose the directly seeded item after a scan.")
        return item_id


def _find_matching_item_id(list_response: dict[str, Any], title: str, author: str | None) -> str | None:
    matches = _find_matching_item_ids(list_response, title, author)
    return matches[0] if matches else None


def _find_matching_item_ids(list_response: dict[str, Any], title: str, author: str | None) -> list[str]:
    items = list_response.get("results") or list_response.get("items") or []
    matches: list[str] = []
    for item in items:
        metadata = (item.get("media") or {}).get("metadata") or {}
        item_title = metadata.get("title") or ""
        if title.lower() not in item_title.lower():
            continue
        if author:
            item_author = metadata.get("authorName") or ""
            if item_author and author.lower() not in item_author.lower():
                continue
        item_id = item.get("id")
        if isinstance(item_id, str):
            matches.append(item_id)
    return matches
