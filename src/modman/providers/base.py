"""Provider interface and shared HTTP helpers (retry/backoff, a shared session)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import requests

from ..model import Mod, ResolvedVersion

USER_AGENT = "modman/0.1 (+https://github.com/local/modman)"


class ResolveError(Exception):
    """A version could not be resolved (not found, no matching build, API error)."""


class ManualRequired(Exception):
    """Resolution succeeded but the file must be downloaded by hand.

    Carries the info needed to tell the user where to get it.
    """

    def __init__(self, message: str, *, url: str | None = None, filename: str | None = None):
        super().__init__(message)
        self.url = url
        self.filename = filename


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
    backoff: float = 1.5,
    timeout: float = 30,
):
    """HTTP request returning parsed JSON, with retry on transient failures.

    Retries 429/5xx and connection errors with exponential backoff. 4xx (other
    than 429) are raised immediately as ``requests.HTTPError``.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.request(
                method, url, params=params, headers=headers, timeout=timeout
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.HTTPError(
                    f"{resp.status_code} {resp.reason}", response=resp
                )
                _sleep_retry(resp, attempt, backoff)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff ** attempt)
                continue
            raise ResolveError(f"network error for {url}: {e}") from e
    assert last_exc is not None
    raise ResolveError(f"request failed after {retries} retries: {last_exc}") from last_exc


def _sleep_retry(resp: requests.Response, attempt: int, backoff: float) -> None:
    retry_after = resp.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        time.sleep(min(int(retry_after), 30))
    else:
        time.sleep(backoff ** attempt)


class Provider(ABC):
    name: str

    @abstractmethod
    def resolve(
        self,
        mod: Mod,
        game_version: str,
        loader: str,
        session: requests.Session,
    ) -> ResolvedVersion:
        """Resolve ``mod`` to a concrete version.

        Raises ``ResolveError`` if nothing matches, or ``ManualRequired`` if a
        version exists but must be downloaded by hand.
        """
        raise NotImplementedError

    def download_headers(self) -> dict:
        """Extra headers needed to download this provider's files (e.g. API key)."""
        return {}
