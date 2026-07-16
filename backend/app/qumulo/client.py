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
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
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
                    if resp.status_code >= 500 and attempt < MAX_RETRIES:
                        last_err = err
                        time.sleep(RETRY_BASE_DELAY * (2**attempt))
                        continue
                    raise err
                return resp.json()
            except ApiError:
                raise
            except httpx.TimeoutException as e:
                raise ApiTimeout(str(e)) from e
            except (httpx.NetworkError, httpx.TransportError) as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY * (2**attempt))
                    continue
                raise
        raise last_err  # type: ignore[misc]

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


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
