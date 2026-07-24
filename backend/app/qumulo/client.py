"""Synchronous Qumulo REST client backed by httpx.

Runs in thread-pool workers (never on the asyncio event loop directly).
The Protocol matches qsnap's Client so the compute layer can use it unchanged.
"""

import time
import warnings
from dataclasses import dataclass
from typing import Protocol

import httpx

MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5

# Network-level failures (DNS blips, connection resets) get a longer, capped
# backoff than 5xx responses -- they're often transient host/resolver hiccups
# lasting several seconds, longer than a 500 is usually worth waiting out.
NETWORK_MAX_RETRIES = 6
NETWORK_RETRY_MAX_DELAY = 8.0


class Client(Protocol):
    def request(self, method: str, path: str, body: dict | None = None) -> dict: ...


@dataclass
class ApiError(Exception):
    status_code: int
    error_class: str
    description: str

    def __str__(self) -> str:
        return f"[{self.status_code}] {self.error_class}: {self.description}"

    def is_snapshot_not_found(self) -> bool:
        return self.status_code == 404 and "snapshot_not_found" in self.error_class


class ApiTimeout(Exception):
    pass


class QumuloClient:
    def __init__(
        self,
        host: str,
        port: int = 8000,
        token: str = "",
        insecure: bool = False,
        timeout: float = 300.0,
    ) -> None:
        if insecure:
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        self._client = httpx.Client(
            base_url=f"https://{host}:{port}",
            headers={"Authorization": f"Bearer {token}"},
            verify=not insecure,
            timeout=timeout,
        )

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        http_attempt = 0
        network_attempt = 0
        while True:
            try:
                resp = self._client.request(method, path, json=body)
                if resp.status_code >= 400:
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                    err = ApiError(
                        status_code=resp.status_code,
                        error_class=data.get("error_class", ""),
                        description=data.get("description", f"HTTP {resp.status_code}"),
                    )
                    if resp.status_code >= 500 and http_attempt < MAX_RETRIES:
                        time.sleep(RETRY_BASE_DELAY * (2**http_attempt))
                        http_attempt += 1
                        continue
                    raise err
                return resp.json()
            except ApiError:
                raise
            except httpx.TimeoutException as e:
                raise ApiTimeout(f"The cluster did not respond in time ({e}).") from e
            except (httpx.NetworkError, httpx.TransportError) as e:
                if network_attempt < NETWORK_MAX_RETRIES:
                    delay = min(RETRY_BASE_DELAY * (2**network_attempt), NETWORK_RETRY_MAX_DELAY)
                    time.sleep(delay)
                    network_attempt += 1
                    continue
                raise ConnectionError(describe_connection_error(e)) from e

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def describe_connection_error(e: Exception) -> str:
    """Translate a raw network exception (often a bare OS errno string, e.g.
    "[Errno -3] Temporary failure in name resolution") into something a user
    can act on -- this always means the retry budget above was exhausted, so
    it's a longer-than-transient network/DNS problem, not a one-off blip."""
    return f"Lost connection to the cluster ({e}). This is usually a temporary network or DNS issue -- try again in a moment."


def login(
    host: str, port: int, username: str, password: str, insecure: bool = False, timeout: float = 30.0
) -> str:
    """Exchange Qumulo username/password for a session bearer token via /v1/session/login."""
    if insecure:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    with httpx.Client(base_url=f"https://{host}:{port}", verify=not insecure, timeout=timeout) as client:
        resp = client.post("/v1/session/login", json={"username": username, "password": password})
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except Exception:
                data = {}
            raise ApiError(
                status_code=resp.status_code,
                error_class=data.get("error_class", ""),
                description=data.get("description", f"HTTP {resp.status_code}"),
            )
        return resp.json()["bearer_token"]


def _bearer_request(
    method: str, host: str, port: int, bearer_token: str, path: str,
    body: dict | None = None, insecure: bool = False, timeout: float = 30.0,
):
    if insecure:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    with httpx.Client(
        base_url=f"https://{host}:{port}",
        headers={"Authorization": f"Bearer {bearer_token}"},
        verify=not insecure,
        timeout=timeout,
    ) as client:
        resp = client.request(method, path, json=body)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except Exception:
                data = {}
            raise ApiError(
                status_code=resp.status_code,
                error_class=data.get("error_class", ""),
                description=data.get("description", f"HTTP {resp.status_code}"),
            )
        return resp.json() if resp.content else {}


def who_am_i(host: str, port: int, bearer_token: str, insecure: bool = False, timeout: float = 30.0) -> dict:
    """The identity a bearer token authenticates as -- used to derive the
    identity reference /v1/auth/access-tokens/ needs (its "user" field
    requires a name/auth_id/sid/uid/gid, not the plain "id" who-am-i also
    returns; "sid" is the one that's worked in testing)."""
    return _bearer_request("GET", host, port, bearer_token, "/v1/session/who-am-i", insecure=insecure, timeout=timeout)


def create_access_token(
    host: str, port: int, bearer_token: str, identity: dict,
    expiration_time: str | None, insecure: bool = False, timeout: float = 30.0,
) -> tuple[str, str]:
    """Create a Qumulo access token (distinct from -- and not subject to the
    fixed lifetime of -- a /v1/session/login session token). expiration_time
    is an optional ISO 8601 'Z' string; omitting it creates a token that
    never expires. Returns (token_id, bearer_token)."""
    body: dict = {"user": identity}
    if expiration_time is not None:
        body["expiration_time"] = expiration_time
    result = _bearer_request(
        "POST", host, port, bearer_token, "/v1/auth/access-tokens/", body, insecure=insecure, timeout=timeout
    )
    return result["id"], result["bearer_token"]


def revoke_access_token(
    host: str, port: int, bearer_token: str, token_id: str, insecure: bool = False, timeout: float = 30.0
) -> None:
    """Revoke an access token this app created. Raises ApiError/etc on
    failure like every other function here -- callers that consider this
    best-effort (e.g. replacing a cluster's credentials) are responsible for
    swallowing that themselves, same as any other "nice to have" cleanup."""
    _bearer_request(
        "DELETE", host, port, bearer_token, f"/v1/auth/access-tokens/{token_id}", insecure=insecure, timeout=timeout
    )
