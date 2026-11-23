export default function Loading() {
  return <main className="min-h-screen bg-slate-950 text-slate-200"><div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8" aria-busy="true">
    <div className="h-3 w-24 animate-pulse rounded bg-slate-800" />
    <div className="mt-6 h-8 w-56 animate-pulse rounded bg-slate-800" />
    <div className="mt-6 h-40 animate-pulse rounded-2xl border border-slate-800 bg-slate-900/40" />
    <div className="mt-6 h-64 animate-pulse rounded-2xl border border-slate-800 bg-slate-900/40" />
    <div className="mt-6 h-48 animate-pulse rounded-2xl border border-slate-800 bg-slate-900/40" />
    <p className="sr-only">Loading security evidence, TVL behaviour and the TVL explanation…</p>
  </div></main>;
}
