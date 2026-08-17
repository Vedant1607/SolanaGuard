import "dotenv/config";
import { PrismaClient } from "../generated/client/client";
import { PrismaPg } from "@prisma/adapter-pg";

const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL });
const prisma = new PrismaClient({ adapter });

const LAUNCH_PROTOCOLS = [
  { slug: "raydium",  name: "Raydium",     category: "DEX" as const,            programId: "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8" },
  { slug: "orca",     name: "Orca",        category: "DEX" as const,            programId: "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc" },
  { slug: "kamino",   name: "Kamino Lend", category: "LENDING" as const,        programId: "KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF" },
  { slug: "marginfi", name: "Marginfi",    category: "LENDING" as const,        programId: "MFv2hWf31Z9kbCa1snEPdcgp168vLLAZnkTHsMgGgAB" },
  { slug: "marinade", name: "Marinade",    category: "LIQUID_STAKING" as const, programId: "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD" },
  { slug: "jito",     name: "Jito",        category: "LIQUID_STAKING" as const, programId: "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb" },
];

async function main() {
  console.log("🌱 Seeding launch protocols...");
  for (const p of LAUNCH_PROTOCOLS) {
    await prisma.protocol.upsert({ where: { slug: p.slug }, create: p, update: p });
    console.log(`  ✓ ${p.name}`);
  }
  console.log("✅ Seed complete");
}

main()
  .catch((e) => { console.error(e); process.exit(1); })
  .finally(async () => await prisma.$disconnect());