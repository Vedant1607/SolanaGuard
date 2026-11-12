import "dotenv/config";
import { PrismaClient } from "../generated/client/client";
import { PrismaPg } from "@prisma/adapter-pg";

const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL });
const prisma = new PrismaClient({ adapter });

// Must stay in sync with migration 20261112120100_phase1_catalogue_data.
// Marginfi is kept (history preserved) but inactive; Drift is PARTIAL.
const LAUNCH_PROTOCOLS = [
  { slug: "raydium",  name: "Raydium",     category: "DEX" as const,            programId: "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", isActive: true },
  { slug: "orca",     name: "Orca",        category: "DEX" as const,            programId: "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", isActive: true },
  { slug: "kamino",   name: "Kamino Lend", category: "LENDING" as const,        programId: "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD", isActive: true },
  { slug: "marginfi", name: "Marginfi",    category: "LENDING" as const,        programId: "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA", isActive: false },
  { slug: "marinade", name: "Marinade",    category: "LIQUID_STAKING" as const, programId: "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD", isActive: true },
  { slug: "jito",     name: "Jito",        category: "LIQUID_STAKING" as const, programId: "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb", isActive: true },
  { slug: "drift",    name: "Drift",       category: "PERPS" as const,          programId: "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH", isActive: true },
];

const REGISTRY = [
  { slug: "raydium", address: "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", kind: "PROGRAM" as const, label: "Raydium AMM v4", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "raydium", address: "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", kind: "PROGRAM" as const, label: "Raydium CLMM", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "raydium", address: "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", kind: "PROGRAM" as const, label: "Raydium CPMM", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "orca", address: "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", kind: "PROGRAM" as const, label: "Orca Whirlpools", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "kamino", address: "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD", kind: "PROGRAM" as const, label: "Kamino Lend", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "kamino", address: "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF", kind: "MARKET" as const, label: "Kamino main lending market", isSharedDependency: false, expectedOwner: "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: owned by expected program KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD" },
  { slug: "marinade", address: "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD", kind: "PROGRAM" as const, label: "Marinade Liquid Staking", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "marinade", address: "8szGkuLTAux9XMgZ2vtY39jVSowEcpBfFfD8hXSEqdGC", kind: "MARKET" as const, label: "Marinade state account", isSharedDependency: false, expectedOwner: "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: owned by expected program MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD" },
  { slug: "marinade", address: "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So", kind: "MINT" as const, label: "mSOL mint", isSharedDependency: false, expectedOwner: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: owned by expected program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" },
  { slug: "jito", address: "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb", kind: "STAKE_POOL" as const, label: "Jito stake pool", isSharedDependency: false, expectedOwner: "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: owned by expected program SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy" },
  { slug: "jito", address: "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy", kind: "PROGRAM" as const, label: "SPL Stake Pool program", isSharedDependency: true, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "jito", address: "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", kind: "MINT" as const, label: "JitoSOL mint", isSharedDependency: false, expectedOwner: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: owned by expected program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" },
  { slug: "drift", address: "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH", kind: "PROGRAM" as const, label: "Drift v2", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: true, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
  { slug: "marginfi", address: "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA", kind: "PROGRAM" as const, label: "Marginfi v2", isSharedDependency: false, expectedOwner: "BPFLoaderUpgradeab1e11111111111111111111111", isActive: false, verificationNote: "mainnet getAccountInfo 2026-11-11: executable, owned by BPF upgradeable loader; ProgramData header decoded" },
];
const VERIFIED_AT = new Date("2026-11-11T18:56:33+00:00");

async function main() {
  console.log("🌱 Seeding launch protocols...");
  const ids = new Map<string, string>();
  for (const p of LAUNCH_PROTOCOLS) {
    const row = await prisma.protocol.upsert({ where: { slug: p.slug }, create: p, update: p });
    ids.set(p.slug, row.id);
    console.log(`  ✓ ${p.name}${p.isActive ? "" : " (inactive)"}`);
  }

  console.log("🌱 Seeding address registry...");
  for (const { slug, ...entry } of REGISTRY) {
    const protocolId = ids.get(slug)!;
    const data = { ...entry, verification: "VERIFIED" as const, verifiedAt: VERIFIED_AT };
    await prisma.protocolAddress.upsert({
      where: { protocolId_address: { protocolId, address: entry.address } },
      create: { protocolId, ...data },
      update: data,
    });
    console.log(`  ✓ ${slug}: ${entry.label}`);
  }
  console.log("✅ Seed complete");
}

main()
  .catch((e) => { console.error(e); process.exit(1); })
  .finally(async () => await prisma.$disconnect());
