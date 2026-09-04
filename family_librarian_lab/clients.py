"""Real CWA and Audiobookshelf helpers for the lab's cwa-local/abs profiles.

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
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CWA_SERVICE = "cwa"
CWA_PROFILE = "cwa-local"
CWA_INTERNAL_URL = "http://cwa:8083"
CWA_DEFAULT_IMAGE = "crocodilestick/calibre-web-automated:latest"
CWA_DEFAULT_HOST_PORT = 18083
CWA_DEFAULT_USERNAME = "admin"
CWA_DEFAULT_PASSWORD = "admin123"
# CWA ships this account by default -- unrelated to any real deployment's
# credentials, and every scenario run is a fresh, disposable CWA instance.
CWA_INGEST_CONTAINER_PATH = "/cwa-ingest"

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
CWA_SFTP_DEFAULT_PASSWORD = "family-librarian-lab-sftp-only"
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
ABS_DEFAULT_PASSWORD = "admin123"
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
# (SMTP-03) rather than faking one.
SMTP_AUTH_USERNAME = "labmailer"
SMTP_AUTH_PASSWORD = "family-librarian-lab-smtp-only"

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


@dataclass(slots=True)
class MailpitClient:
    """Drives Mailpit's own HTTP API to independently verify SMTP delivery --
    the same 'assert against the real destination, not Family Librarian's own
    state' pattern AbsClient/CwaClient already follow. Mailpit's `Username`
    field on a stored message is the SMTP-AUTH identity that actually
    authenticated the send, so a case can confirm both that a message
    arrived AND that it arrived authenticated as SMTP_AUTH_USERNAME, not
    merely that *some* connection reached the catcher."""

    host_base_url: str

    def ready(self) -> bool:
        status, _ = _http(f"{self.host_base_url}/api/v1/messages?limit=1")
        return status == 200

    def clear(self) -> None:
        """Delete every stored message -- call at the start of a case so an
        earlier case's leftover mail (Mailpit's own state persists for the
        life of the container, shared across every case in the suite unless
        cleared) can never be mistaken for this case's delivery."""
        _http(f"{self.host_base_url}/api/v1/messages", method="DELETE", json_body={})

    def find_message(
        self, *, to: str, subject_contains: str | None = None, timeout_seconds: float = 15.0
    ) -> dict[str, Any] | None:
        """Polls Mailpit's message list for one already delivered to `to`
        (and, if given, whose subject contains `subject_contains`). Returns
        the message summary (includes `Username`, the authenticated SMTP-AUTH
        identity) or None if nothing matched within the deadline -- callers
        proving a *negative* (SMTP-03's rejected-auth case) should pass a
        short timeout instead of waiting out the full default."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            status, body = _http(f"{self.host_base_url}/api/v1/messages")
            if status == 200:
                for summary in json.loads(body).get("messages", []):
                    recipients = [
                        address.get("Address")
                        for address in summary.get("To") or []
                        if isinstance(address, dict)
                    ]
                    subject = summary.get("Subject") or ""
                    if to in recipients and (subject_contains is None or subject_contains in subject):
                        return summary
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)
