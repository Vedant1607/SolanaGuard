import base64
import os
import struct
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.solana.base58 import b58decode, b58encode, is_valid_pubkey
from app.solana.loader import (
    LoaderDecodeError,
    decode_program_account,
    decode_programdata_header,
)
from tests.solana_fixtures import account_value, recorded_responses

RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_AMM_PROGRAMDATA = "A7ZG7ByDi8TF8ujAX9vSAVCTXK1HHpJ6ZqC6wPRYCjUR"


def recorded_bytes(address: str, length: int) -> bytes:
    response = recorded_responses()[(address, 0, length)]
    return base64.b64decode(account_value(response)["data"][0])


class Base58Tests(unittest.TestCase):
    def test_round_trip_of_real_program_ids(self):
        for address in (RAYDIUM_AMM, "BPFLoaderUpgradeab1e11111111111111111111111", "11111111111111111111111111111111"):
            self.assertEqual(b58encode(b58decode(address)), address)
            self.assertTrue(is_valid_pubkey(address))

    def test_invalid_keys_are_rejected(self):
        # The old Kamino seed value decodes to 33 bytes; RPC rejected it too.
        self.assertFalse(is_valid_pubkey("KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF"))
        self.assertFalse(is_valid_pubkey("not-base58-0OIl"))
        self.assertTrue(is_valid_pubkey("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"))


class LoaderDecoderTests(unittest.TestCase):
    def test_program_account_yields_programdata_address(self):
        data = recorded_bytes(RAYDIUM_AMM, 36)
        self.assertEqual(decode_program_account(data), self._programdata_of(RAYDIUM_AMM))

    def _programdata_of(self, program):
        return next(
            addr for (addr, _, length) in recorded_responses()
            if length == 45 and addr == decode_program_account(recorded_bytes(program, 36))
        )

    def test_programdata_header_decodes_recorded_values(self):
        programdata = self._programdata_of(RAYDIUM_AMM)
        header = decode_programdata_header(recorded_bytes(programdata, 45))
        self.assertEqual(header.last_deploy_slot, 445_763_204)
        self.assertEqual(header.upgrade_authority, "FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK")

    def test_every_recorded_programdata_header_decodes(self):
        decoded = [
            decode_programdata_header(base64.b64decode(account_value(r)["data"][0]))
            for (_, _, length), r in recorded_responses().items()
            if length == 45
        ]
        self.assertEqual(len(decoded), 9)
        self.assertTrue(all(h.upgrade_authority for h in decoded))

    def test_immutable_header_has_no_authority(self):
        data = struct.pack("<IQ", 3, 123) + b"\x00" + bytes(32)
        header = decode_programdata_header(data)
        self.assertIsNone(header.upgrade_authority)
        self.assertEqual(header.last_deploy_slot, 123)

    def test_malformed_headers_raise_instead_of_guessing(self):
        good = recorded_bytes(self._programdata_of(RAYDIUM_AMM), 45)
        cases = {
            "DECODE_TRUNCATED": good[:20],
            "DECODE_UNEXPECTED_TAG": struct.pack("<I", 2) + good[4:],
            "DECODE_INVALID_OPTION_FLAG": good[:12] + b"\x07" + good[13:],
        }
        for code, data in cases.items():
            with self.subTest(code=code), self.assertRaises(LoaderDecodeError) as raised:
                decode_programdata_header(data)
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(LoaderDecodeError):
            decode_program_account(b"\x02\x00")
        with self.assertRaises(LoaderDecodeError):
            decode_program_account(good)  # a ProgramData header is not a Program header


if __name__ == "__main__":
    unittest.main()
