"""On-chain security adapter: upgradeability, upgrade authority, deploy slot.

For every registered address it reads the account owner (``registry.owner``),
so the registry can be re-verified. For PROGRAM addresses it also reads the
BPF upgradeable-loader headers:

* ``security.program_upgradeable`` (unit ``bool``: 1 upgradeable, 0 immutable)
* ``security.upgrade_authority``   (unit ``pubkey``; emitted only when the
  program is upgradeable or when upgradeability itself could not be measured)
* ``security.last_deploy_slot``    (unit ``slot``)

Two ``getAccountInfo`` calls per program, each requesting a header slice of at
most 45 bytes. Every observation carries the RPC context slot it was read at.
``source_observed_at`` stays NULL: RPC reports a slot, not a wall-clock time,
and the collection time is recorded separately as ``collectedAt``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.adapters.base import (
    AdapterContext,
    AddressKind,
    Observation,
    ObservationStatus,
    RegisteredAddress,
)
from app.solana.account_reader import AccountReadError, AccountReader
from app.solana.loader import (
    BPF_UPGRADEABLE_LOADER,
    LEGACY_IMMUTABLE_LOADERS,
    PROGRAM_HEADER_LEN,
    PROGRAMDATA_HEADER_LEN,
    LoaderDecodeError,
    decode_program_account,
    decode_programdata_header,
)

SOURCE = "solana-rpc"

METRIC_OWNER = "registry.owner"
METRIC_UPGRADEABLE = "security.program_upgradeable"
METRIC_AUTHORITY = "security.upgrade_authority"
METRIC_DEPLOY_SLOT = "security.last_deploy_slot"

UNITS = {
    METRIC_OWNER: "pubkey",
    METRIC_UPGRADEABLE: "bool",
    METRIC_AUTHORITY: "pubkey",
    METRIC_DEPLOY_SLOT: "slot",
}
PROGRAM_METRICS = (METRIC_UPGRADEABLE, METRIC_AUTHORITY, METRIC_DEPLOY_SLOT)


class OnchainSecurityAdapter:
    key = "onchain_security"
    version = "1"
    interval = timedelta(minutes=15)

    def __init__(self, reader: AccountReader):
        self._reader = reader

    def applies_to(self, address: RegisteredAddress) -> bool:
        return True  # every address gets an owner check; programs get more

    async def collect(self, ctx: AdapterContext) -> list[Observation]:
        observations: list[Observation] = []
        for address in ctx.addresses:
            if not self.applies_to(address):
                continue
            if address.kind is AddressKind.PROGRAM:
                observations.extend(await self._collect_program(address))
            else:
                observations.extend(await self._collect_owner_only(address))
        return observations

    # -- helpers -----------------------------------------------------------

    def _ok(self, address: RegisteredAddress, metric: str, slot: int, *, numeric=None, text=None) -> Observation:
        return Observation(
            metric_key=metric,
            status=ObservationStatus.OK,
            unit=UNITS[metric],
            source=SOURCE,
            value_numeric=None if numeric is None else Decimal(numeric),
            value_text=text,
            address_id=address.id,
            source_slot=slot,
        )

    def _failed(
        self,
        address: RegisteredAddress,
        metrics: tuple[str, ...],
        code: str,
        *,
        slot: int | None = None,
        missing: bool = False,
    ) -> list[Observation]:
        return [
            Observation.failed(
                metric, UNITS[metric], SOURCE, code, address_id=address.id, source_slot=slot, missing=missing
            )
            for metric in metrics
        ]

    async def _collect_owner_only(self, address: RegisteredAddress) -> list[Observation]:
        try:
            lookup = await self._reader.get_account_slice(address.address, 0, 0)
        except AccountReadError as error:
            return self._failed(address, (METRIC_OWNER,), error.code)
        if lookup.account is None:
            return self._failed(
                address, (METRIC_OWNER,), "ACCOUNT_NOT_FOUND", slot=lookup.context_slot, missing=True
            )
        return [self._ok(address, METRIC_OWNER, lookup.context_slot, text=lookup.account.owner)]

    async def _collect_program(self, address: RegisteredAddress) -> list[Observation]:
        all_metrics = (METRIC_OWNER, *PROGRAM_METRICS)
        try:
            lookup = await self._reader.get_account_slice(address.address, 0, PROGRAM_HEADER_LEN)
        except AccountReadError as error:
            return self._failed(address, all_metrics, error.code)
        if lookup.account is None:
            return self._failed(address, all_metrics, "ACCOUNT_NOT_FOUND", slot=lookup.context_slot, missing=True)

        program = lookup.account
        observations = [self._ok(address, METRIC_OWNER, program.context_slot, text=program.owner)]

        if program.owner in LEGACY_IMMUTABLE_LOADERS:
            # Legacy loaders have no upgrade mechanism and no ProgramData account.
            observations.append(self._ok(address, METRIC_UPGRADEABLE, program.context_slot, numeric=0))
            observations.extend(
                self._failed(
                    address,
                    (METRIC_DEPLOY_SLOT,),
                    "NOT_APPLICABLE_LEGACY_LOADER",
                    slot=program.context_slot,
                    missing=True,
                )
            )
            return observations

        if program.owner != BPF_UPGRADEABLE_LOADER:
            observations.extend(
                self._failed(address, PROGRAM_METRICS, "UNSUPPORTED_LOADER", slot=program.context_slot)
            )
            return observations

        try:
            programdata_address = decode_program_account(program.data)
        except LoaderDecodeError as error:
            observations.extend(self._failed(address, PROGRAM_METRICS, error.code, slot=program.context_slot))
            return observations

        try:
            pd_lookup = await self._reader.get_account_slice(programdata_address, 0, PROGRAMDATA_HEADER_LEN)
        except AccountReadError as error:
            observations.extend(self._failed(address, PROGRAM_METRICS, error.code))
            return observations
        if pd_lookup.account is None:
            observations.extend(
                self._failed(
                    address, PROGRAM_METRICS, "PROGRAMDATA_NOT_FOUND", slot=pd_lookup.context_slot, missing=True
                )
            )
            return observations
        if pd_lookup.account.owner != BPF_UPGRADEABLE_LOADER:
            observations.extend(
                self._failed(address, PROGRAM_METRICS, "PROGRAMDATA_OWNER_MISMATCH", slot=pd_lookup.context_slot)
            )
            return observations

        try:
            header = decode_programdata_header(pd_lookup.account.data)
        except LoaderDecodeError as error:
            observations.extend(self._failed(address, PROGRAM_METRICS, error.code, slot=pd_lookup.context_slot))
            return observations

        slot = pd_lookup.context_slot
        upgradeable = header.upgrade_authority is not None
        observations.append(self._ok(address, METRIC_UPGRADEABLE, slot, numeric=1 if upgradeable else 0))
        if upgradeable:
            observations.append(self._ok(address, METRIC_AUTHORITY, slot, text=header.upgrade_authority))
        observations.append(self._ok(address, METRIC_DEPLOY_SLOT, slot, numeric=header.last_deploy_slot))
        return observations
