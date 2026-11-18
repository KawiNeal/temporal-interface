"""Async client for the Temporal Configuration Service API.

This is the only module that talks HTTP. The UI deals only with the typed
models and the single ``ApiError`` exception defined here.

Contents:
  - Part, Configuration, AllowableStatuses : typed versions of the API's JSON
  - ApiError                               : the one exception the UI has to handle
  - _error_message()                       : turns the API's different error formats into text
  - ConfigServiceClient                    : one method per endpoint, plus a part cache
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# Logs one line per API call, e.g. "GET /api/parts/e78a… -> 200 (142 ms)".
# Silent unless the app enables it (LOG_API=1 in .env). The API key is never logged.
log = logging.getLogger("temporal.api")

# All statuses listed in the Swagger spec. Not used by the code yet; kept as reference.
STATUSES = (
    "configuration", "active", "draft", "deprecated",
    "obsolete", "experimental", "retired", "recalled",
)


# ---- models --------------------------------------------------------------------
# frozen=True makes these read-only. A Part sitting in the cache can't be changed by
# accident somewhere in the UI; to change a part you must go through the API.
# Each from_json() is the ONE place that maps the API's camelCase names (partNo,
# partUuid) to Python's snake_case, so an API rename is fixed in one spot.

@dataclass(frozen=True)
class Part:
    """Blueprint definition of a component (shared by many configurations)."""
    uuid: str
    part_no: str
    name: str
    status: str
    unit: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Part:
        return cls(uuid=data["uuid"], part_no=data["partNo"], name=data["name"],
                   status=data["status"], unit=data["unit"])


@dataclass(frozen=True)
class Configuration:
    """A concrete node in an assembly tree that references a Part."""
    uuid: str
    part_uuid: str
    quantity: int
    end_unit_serial_no: str | None = None   # only root configurations (whole time machines) have one

    @property
    def is_end_unit(self) -> bool:
        # The rule "a root is a configuration with a serial number" lives here, not in the UI.
        return self.end_unit_serial_no is not None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Configuration:
        return cls(uuid=data["uuid"], part_uuid=data["partUuid"],
                   quantity=data["quantity"],
                   end_unit_serial_no=data.get("endUnitSerialNo"))   # .get(): missing or null -> None


@dataclass(frozen=True)
class AllowableStatuses:
    part_uuid: str
    current_status: str
    allowable: list[str]

    @property
    def transitions(self) -> list[str]:
        """Statuses the part can move *to* (excludes the current one)."""
        # The API's list INCLUDES the current status (e.g. draft -> [draft, experimental]).
        # The dropdown should only offer real changes, so remove it.
        return [s for s in self.allowable if s != self.current_status]


# ---- errors --------------------------------------------------------------------

@dataclass
class ApiError(Exception):
    """Any failure talking to the API, normalised into one shape."""
    status_code: int | None          # HTTP status, or None for network failures and timeouts
    message: str                     # human-readable reason, safe to show the user
    allowable_statuses: list[str] = field(default_factory=list)   # filled when a status change is rejected

    def __str__(self) -> str:
        code = f"HTTP {self.status_code}: " if self.status_code else ""
        return f"{code}{self.message}"


def _error_message(response: httpx.Response) -> tuple[str, list[str]]:
    """Extract a readable message from the API's several error shapes.

    Seen in practice:
      {"error": "Part not found"}
      {"error": "Invalid status change", "allowableStatuses": [...]}
      {"success": false, "error": {"name": "ZodError", "message": "..."}}
      {"message": "Forbidden"}
    """
    try:
        body = response.json()
    except ValueError:
        # Not JSON at all (e.g. an HTML error page): fall back to the raw text.
        return (response.text or response.reason_phrase or "Unknown error"), []
    if not isinstance(body, dict):
        return str(body), []
    allowable = body.get("allowableStatuses") or []
    error = body.get("error")
    if isinstance(error, dict):
        if error.get("name") == "ZodError":
            # Validation-library output (e.g. a malformed UUID). Its raw message is a long
            # technical dump, so replace it with something a technician can understand.
            return "The request was rejected as invalid (bad ID or value).", allowable
        return str(error.get("message") or error), allowable
    # {"error": "..."} from the service, or {"message": "..."} from AWS API Gateway (bad key).
    return str(error or body.get("message") or "Unknown error"), allowable


# ---- client --------------------------------------------------------------------

class ConfigServiceClient:
    """Thin async wrapper around the REST endpoints, with a part cache.

    Parts are cached by UUID because the same part (e.g. "Power Cell") appears
    in many configurations; this avoids re-fetching it for every tree node.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport: normally None (real network). Tests pass httpx.MockTransport here,
        # so the client can be tested with fake responses and no internet.
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # Set once here so every request carries the API key automatically.
            headers={"x-api-key": api_key, "accept": "application/json"},
            # A hung API becomes an error after 15 s instead of a frozen screen.
            timeout=timeout,
            transport=transport,
        )
        self._parts: dict[str, Part] = {}   # the part cache: {part uuid: Part}

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Every API call goes through here: send, time it, log it, and turn any failure
        into ApiError, so the rest of the app never sees an httpx exception."""
        # Build a readable description of the call for the log (never includes the key).
        target = path
        if kwargs.get("params"):
            target += "?" + "&".join(f"{k}={v}" for k, v in kwargs["params"].items())
        body = f" body={json.dumps(kwargs['json'])}" if "json" in kwargs else ""
        started = time.perf_counter()

        def elapsed() -> str:
            return f"{(time.perf_counter() - started) * 1000:.0f} ms"

        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            log.warning("%s %s%s -> TIMEOUT (%s)", method, target, body, elapsed())
            # "raise ... from exc" keeps the original error attached, for debugging.
            raise ApiError(None, "The service took too long to respond.") from exc
        except httpx.HTTPError as exc:
            # DNS failure, connection refused, no internet, ...
            log.warning("%s %s%s -> %s (%s)", method, target, body, exc.__class__.__name__, elapsed())
            raise ApiError(None, f"Could not reach the service ({exc.__class__.__name__}).") from exc
        if response.is_error:
            # The service answered, but with 4xx/5xx.
            message, allowable = _error_message(response)
            log.warning("%s %s%s -> %d %s (%s)", method, target, body,
                        response.status_code, message, elapsed())
            raise ApiError(response.status_code, message, allowable)
        log.info("%s %s%s -> %d (%s)", method, target, body, response.status_code, elapsed())
        return response.json()

    # ---- endpoints -------------------------------------------------------

    async def health(self) -> bool:
        # Only used for the "API online" chip, so it answers True/False and never raises.
        try:
            data = await self._request("GET", "/api/health")
        except ApiError:
            return False
        return data.get("status") == "ok"

    async def get_children(self, parent_uuid: str | None = None) -> list[Configuration]:
        """Child configurations of a parent; with no parent, the end units (roots)."""
        params = {"parentUuid": parent_uuid} if parent_uuid else None   # httpx builds "?parentUuid=..."
        data = await self._request("GET", "/api/configs/children", params=params)
        return [Configuration.from_json(item) for item in data]

    async def get_part(self, uuid: str, *, refresh: bool = False) -> Part:
        # Cached: many configurations share a part (all 20 time machines use TM-1000),
        # so without the cache the same part would be fetched again and again.
        # refresh=True skips the cache when fresh data matters (the details panel).
        if not refresh and uuid in self._parts:
            return self._parts[uuid]
        part = Part.from_json(await self._request("GET", f"/api/parts/{uuid}"))
        self._parts[uuid] = part
        return part

    async def get_allowable_statuses(self, uuid: str) -> AllowableStatuses:
        # Deliberately NOT cached: what a part may change to depends on related parts,
        # which other technicians may have changed since.
        data = await self._request("GET", f"/api/parts/{uuid}/allowable-statuses")
        return AllowableStatuses(part_uuid=data["partUuid"],
                                 current_status=data["currentStatus"],
                                 allowable=list(data["allowableStatuses"]))

    async def update_part(self, uuid: str, *, status: str | None = None,
                          unit: str | None = None) -> Part:
        # Send only the fields that were given: {"status": "draft"}, not {"status": "draft", "unit": null}.
        body = {k: v for k, v in {"status": status, "unit": unit}.items() if v is not None}
        if not body:
            # A programming mistake, not a user error, so a plain ValueError.
            raise ValueError("Nothing to update: pass status and/or unit.")
        part = Part.from_json(await self._request("PUT", f"/api/parts/{uuid}", json=body))
        self._parts[uuid] = part   # keep the cache in step with what the server now holds
        return part
