"""Pure decoders for BPF upgradeable-loader account headers.

``UpgradeableLoaderState`` is bincode-serialised: a little-endian ``u32``
variant tag followed by the variant's fields.

* ``Program``     (tag 2): ``programdata_address: Pubkey``            -> 36 bytes
* ``ProgramData`` (tag 3): ``slot: u64``,
                           ``upgrade_authority_address: Option<Pubkey>`` -> 45 bytes
  (the option is a 1-byte flag; the 32 key bytes are always reserved)

Only these header bytes are requested from RPC (``dataSlice``), never the
program's executable code. Any unexpected tag, length or flag raises
``LoaderDecodeError``: a malformed header must surface as an ERROR
observation, never as a guessed value.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from app.solana.base58 import b58encode

BPF_UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
# Programs owned by these legacy loaders cannot be upgraded at all.
LEGACY_IMMUTABLE_LOADERS = frozenset(
    {
        "BPFLoader1111111111111111111111111111111111",
        "BPFLoader2111111111111111111111111111111111",
    }
)
LOADER_V4 = "LoaderV411111111111111111111111111111111111"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

PROGRAM_TAG = 2
PROGRAMDATA_TAG = 3
PROGRAM_HEADER_LEN = 36
PROGRAMDATA_HEADER_LEN = 45


class LoaderDecodeError(ValueError):
    """The account bytes are not the expected loader header.

    ``code`` is a short, stable, log-safe identifier.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProgramDataHeader:
    last_deploy_slot: int
    upgrade_authority: str | None  # None => no authority: the program is immutable


def _tag(data: bytes) -> int:
    if len(data) < 4:
        raise LoaderDecodeError("DECODE_TRUNCATED")
    return struct.unpack_from("<I", data, 0)[0]


def decode_program_account(data: bytes) -> str:
    """Return the ProgramData address stored in a ``Program`` account."""
    tag = _tag(data)
    if tag != PROGRAM_TAG:
        raise LoaderDecodeError("DECODE_UNEXPECTED_TAG")
    if len(data) < PROGRAM_HEADER_LEN:
        raise LoaderDecodeError("DECODE_TRUNCATED")
    return b58encode(data[4:36])


def decode_programdata_header(data: bytes) -> ProgramDataHeader:
    """Return the deploy slot and optional upgrade authority of a ``ProgramData`` account."""
    tag = _tag(data)
    if tag != PROGRAMDATA_TAG:
        raise LoaderDecodeError("DECODE_UNEXPECTED_TAG")
    if len(data) < PROGRAMDATA_HEADER_LEN:
        raise LoaderDecodeError("DECODE_TRUNCATED")
    slot = struct.unpack_from("<Q", data, 4)[0]
    flag = data[12]
    if flag == 0:
        authority = None
    elif flag == 1:
        authority = b58encode(data[13:45])
    else:
        raise LoaderDecodeError("DECODE_INVALID_OPTION_FLAG")
    return ProgramDataHeader(last_deploy_slot=slot, upgrade_authority=authority)
