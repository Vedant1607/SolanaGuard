"use client";

import { useState, useTransition } from "react";
import { createTelegramLink } from "@/lib/telegram-link-actions";
import type { TelegramLinkResult } from "@/lib/telegram-link-core";

const FAILURES: Record<Exclude<TelegramLinkResult, { ok: true }>["reason"], string> = {
  unauthenticated: "Sign in to connect Telegram.",
  not_configured: "Telegram alerts are not available right now.",
  no_account: "Your account is still being set up — reload and try again.",
};

/** Requests a fresh single-use link only when clicked; the link is never rendered into the page HTML. */
export function TelegramConnect() {
  const [result, setResult] = useState<TelegramLinkResult | null>(null);
  const [failed, setFailed] = useState(false);
  const [isPending, startTransition] = useTransition();

  const request = () => startTransition(async () => {
    setFailed(false);
    try {
      setResult(await createTelegramLink());
    } catch {
      setResult(null);
      setFailed(true);
    }
  });

  if (result?.ok) {
    return <span className="flex flex-wrap items-center gap-x-2">
      <a href={result.url} target="_blank" rel="noreferrer" className="font-medium text-cyan-300 hover:text-cyan-200">Open Telegram to finish →</a>
      <span className="text-slate-500">Single-use link, expires in 10 min.</span>
    </span>;
  }
  return <span className="flex flex-wrap items-center gap-x-2">
    <button type="button" onClick={request} disabled={isPending} className="font-medium text-cyan-300 hover:text-cyan-200 disabled:opacity-50">{isPending ? "Preparing link…" : "Connect Telegram →"}</button>
    {result && !result.ok && <span className="text-amber-300">{FAILURES[result.reason]}</span>}
    {failed && <span className="text-amber-300">Couldn&apos;t create a link — try again.</span>}
  </span>;
}
