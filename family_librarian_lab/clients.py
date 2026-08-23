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
import time
from dataclasses import dataclass
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

ABS_SERVICE = "abs"
ABS_PROFILE = "abs"
ABS_INTERNAL_URL = "http://abs:80"
ABS_DEFAULT_IMAGE = "advplyr/audiobookshelf:latest"
ABS_DEFAULT_HOST_PORT = 18378
ABS_DEFAULT_USERNAME = "admin"
ABS_DEFAULT_PASSWORD = "family-librarian-lab-abs-only"
ABS_LIBRARY_NAME = "lab-audiobooks"
ABS_LIBRARY_FOLDER_PATH = "/audiobooks"

_BOOK_ID_PATTERN = re.compile(r"/opds/(?:book|download)/(\d+)")


def _http(
    url: str,
    *,
    method: str = "GET",
    json_body: object | None = None,
    basic_auth: tuple[str, str] | None = None,
    bearer_token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, bytes]:
    data = None
    headers: dict[str, str] = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if basic_auth is not None:
        raw = f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(url, data=data, headers=headers, method=method)
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
        import urllib.parse

        url = f"{self.host_base_url}/opds/search/{urllib.parse.quote(title)}"
        status, body = _http(url, basic_auth=(self.username, self.password))
        if status != 200:
            return None
        return _parse_first_matching_book_id(body.decode("utf-8", errors="replace"), title, author)


def _parse_first_matching_book_id(atom_xml: str, title: str, author: str | None) -> str | None:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(atom_xml)
    except ET.ParseError:
        return None

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
                return match.group(1)
    return None


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


def _find_matching_item_id(list_response: dict[str, Any], title: str, author: str | None) -> str | None:
    items = list_response.get("results") or list_response.get("items") or []
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
            return item_id
    return None
