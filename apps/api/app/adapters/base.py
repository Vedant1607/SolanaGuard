"""Protocol-independent adapter contract.

An adapter turns registered protocol addresses into raw ``Observation``s. It
measures; it never interprets (interpretation lives in ``app.risk``) and it
never writes to the database (the collector task does that, per protocol, in
one transaction).

Rules every adapter follows:

1. Emit an observation for every metric it expects for each address it covers.
   A failed measurement becomes an ``ERROR`` or ``MISSING`` observation with a
   sanitised ``error_code``; nothing is silently skipped.
2. Do not raise for expected failures (HTTP errors, timeouts, decode errors).
3. Never invent or default a value: a measurement that was not obtained has no
   value at all, so it can never read as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
import re
from typing import Protocol, runtime_checkable


class ObservationStatus(StrEnum):
    """Values of the PostgreSQL ``ObservationStatus`` enum."""

    OK = "OK"
    MISSING = "MISSING"  # the source has no such datum (e.g. account not found)
    STALE = "STALE"  # the source itself reported data older than it should be
    ERROR = "ERROR"  # the measurement failed (transport, RPC or decode error)


class AddressKind(StrEnum):
    """Values of the PostgreSQL ``AddressKind`` enum."""

    PROGRAM = "PROGRAM"
    STAKE_POOL = "STAKE_POOL"
    MARKET = "MARKET"
    MINT = "MINT"


_ERROR_CODE = re.compile(r"^[A-Z0-9_\-]{1,64}$")
_VALUED = (ObservationStatus.OK, ObservationStatus.STALE)


class InvalidObservation(ValueError):
    pass


@dataclass(frozen=True)
class Observation:
    metric_key: str
    status: ObservationStatus
    unit: str
    source: str
    value_numeric: Decimal | None = None
    value_text: str | None = None
    address_id: str | None = None
    source_observed_at: datetime | None = None
    source_slot: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        # Mirrors the database CHECK constraints on metric_observations, so a
        # bad observation fails loudly in the adapter, not at INSERT time.
        has_value = self.value_numeric is not None or self.value_text is not None
        if self.status in _VALUED and not has_value:
            raise InvalidObservation(f"{self.metric_key}: {self.status} requires a value")
        if self.status not in _VALUED and has_value:
            raise InvalidObservation(f"{self.metric_key}: {self.status} must not carry a value")
        if self.status is ObservationStatus.ERROR and not self.error_code:
            raise InvalidObservation(f"{self.metric_key}: ERROR requires an error_code")
        if self.error_code is not None and not _ERROR_CODE.match(self.error_code):
            # Codes are identifiers, never free text that could hold a URL or key.
            raise InvalidObservation(f"{self.metric_key}: error_code must be a short identifier")
        if self.value_numeric is not None and not isinstance(self.value_numeric, Decimal):
            raise InvalidObservation(f"{self.metric_key}: value_numeric must be a Decimal")
        if not self.metric_key or not self.unit or not self.source:
            raise InvalidObservation("metric_key, unit and source are required")

    @classmethod
    def failed(
        cls,
        metric_key: str,
        unit: str,
        source: str,
        error_code: str,
        *,
        address_id: str | None = None,
        source_slot: int | None = None,
        missing: bool = False,
    ) -> "Observation":
        return cls(
            metric_key=metric_key,
            status=ObservationStatus.MISSING if missing else ObservationStatus.ERROR,
            unit=unit,
            source=source,
            address_id=address_id,
            source_slot=source_slot,
            error_code=error_code,
        )


@dataclass(frozen=True)
class RegisteredAddress:
    id: str
    address: str
    kind: AddressKind
    label: str
    is_shared_dependency: bool = False
    expected_owner: str | None = None


@dataclass(frozen=True)
class AdapterContext:
    protocol_id: str
    slug: str
    addresses: tuple[RegisteredAddress, ...]
    now: datetime


@runtime_checkable
class Adapter(Protocol):
    key: str  # stable identifier stored on every observation
    version: str  # bumped whenever measurement behaviour changes
    interval: timedelta  # intended collection cadence

    def applies_to(self, address: RegisteredAddress) -> bool: ...

    async def collect(self, ctx: AdapterContext) -> list[Observation]: ...
