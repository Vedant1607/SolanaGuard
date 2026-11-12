-- Phase 1 (data only): catalogue corrections and the initial address registry.
--
-- Idempotent and history-preserving. Nothing is deleted. The only existing
-- rows touched are:
--   * protocols.isActive for Marginfi (monitoring stops; every snapshot,
--     risk score, anomaly and watchlist row is kept);
--   * protocols.programId for Kamino and Marginfi, ONLY when the stored value
--     is the known-invalid seed value (Kamino's decodes to 33 bytes and RPC
--     rejects it; Marginfi's has no account on mainnet).
-- Registry rows were checked against mainnet getAccountInfo (owner/loader)
-- when recorded; the raw responses are kept in
-- apps/api/tests/fixtures/solana/mainnet_accounts.json.
--
-- Rollback of the guarded updates, if ever needed:
--   UPDATE "protocols" SET "isActive" = true WHERE "slug" = 'marginfi';
--   UPDATE "protocols" SET "programId" = '<previous value>' WHERE "slug" = '<slug>';

-- Marginfi: stop active monitoring, keep all history.
UPDATE "protocols" SET "isActive" = false
WHERE "slug" = 'marginfi' AND "isActive" = true;

-- Guarded program-ID corrections (no-op unless the known-invalid value is stored
-- and the corrected value is not already used by another row).
UPDATE "protocols" SET "programId" = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'
WHERE "slug" = 'kamino'
  AND "programId" = 'KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF'
  AND NOT EXISTS (SELECT 1 FROM "protocols" WHERE "programId" = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD');

UPDATE "protocols" SET "programId" = 'MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA'
WHERE "slug" = 'marginfi'
  AND "programId" = 'MFv2hWf31Z9kbCa1snEPdcgp168vLLAZnkTHsMgGgAB'
  AND NOT EXISTS (SELECT 1 FROM "protocols" WHERE "programId" = 'MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA');

-- Drift: catalogue entry with PARTIAL support (security monitoring only; no
-- TVL/transaction ingestion is configured for it).
INSERT INTO "protocols" ("id", "slug", "name", "category", "programId", "isActive", "supportLevel")
VALUES (gen_random_uuid()::text, 'drift', 'Drift', 'PERPS', 'dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH', true, 'PARTIAL')
ON CONFLICT DO NOTHING;

-- Initial address registry (14 rows). A row is inserted only when its
-- protocol exists; on a fresh database seed.ts creates the same rows.
-- Marginfi's row is registered inactive.

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8', 'PROGRAM'::"AddressKind", 'Raydium AMM v4', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'raydium'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK', 'PROGRAM'::"AddressKind", 'Raydium CLMM', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'raydium'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C', 'PROGRAM'::"AddressKind", 'Raydium CPMM', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'raydium'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc', 'PROGRAM'::"AddressKind", 'Orca Whirlpools', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'orca'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD', 'PROGRAM'::"AddressKind", 'Kamino Lend', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'kamino'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", '7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF', 'MARKET'::"AddressKind", 'Kamino main lending market', false,
       'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: owned by expected program KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD', true
FROM "protocols" p WHERE p."slug" = 'kamino'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD', 'PROGRAM'::"AddressKind", 'Marinade Liquid Staking', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'marinade'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", '8szGkuLTAux9XMgZ2vtY39jVSowEcpBfFfD8hXSEqdGC', 'MARKET'::"AddressKind", 'Marinade state account', false,
       'MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: owned by expected program MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD', true
FROM "protocols" p WHERE p."slug" = 'marinade'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So', 'MINT'::"AddressKind", 'mSOL mint', false,
       'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: owned by expected program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', true
FROM "protocols" p WHERE p."slug" = 'marinade'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb', 'STAKE_POOL'::"AddressKind", 'Jito stake pool', false,
       'SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: owned by expected program SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy', true
FROM "protocols" p WHERE p."slug" = 'jito'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy', 'PROGRAM'::"AddressKind", 'SPL Stake Pool program', true,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'jito'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn', 'MINT'::"AddressKind", 'JitoSOL mint', false,
       'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: owned by expected program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', true
FROM "protocols" p WHERE p."slug" = 'jito'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH', 'PROGRAM'::"AddressKind", 'Drift v2', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', true
FROM "protocols" p WHERE p."slug" = 'drift'
ON CONFLICT ("protocolId", "address") DO NOTHING;

INSERT INTO "protocol_addresses"
    ("id", "protocolId", "address", "kind", "label", "isSharedDependency", "expectedOwner",
     "verification", "verifiedAt", "verificationNote", "isActive")
SELECT gen_random_uuid()::text, p."id", 'MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA', 'PROGRAM'::"AddressKind", 'Marginfi v2', false,
       'BPFLoaderUpgradeab1e11111111111111111111111', 'VERIFIED'::"AddressVerification", TIMESTAMP '2026-11-11 18:56:33',
       'mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded', false
FROM "protocols" p WHERE p."slug" = 'marginfi'
ON CONFLICT ("protocolId", "address") DO NOTHING;
