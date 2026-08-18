"use client";

import { useState, useTransition } from "react";
import { toggleWatchlist } from "@/lib/watchlist-actions";

export function WatchlistButton({ slug, initialWatched }: { slug: string; initialWatched: boolean }) {
  const [watched, setWatched] = useState(initialWatched);
  const [isPending, startTransition] = useTransition();

  return (
    <button
      disabled={isPending}
      onClick={() => {
        setWatched((w) => !w);
        startTransition(async () => {
          await toggleWatchlist(slug);
        });
      }}
      className={`text-xs px-2 py-1 rounded border transition-colors disabled:opacity-50 ${
        watched
          ? "bg-cyan-500/10 text-cyan-400 border-cyan-500/30"
          : "border-slate-700 text-slate-500 hover:text-slate-300"
      }`}
    >
      {watched ? "★ Watching" : "☆ Watch"}
    </button>
  );
}