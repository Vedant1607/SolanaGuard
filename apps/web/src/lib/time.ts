// The API serialises naive UTC timestamps (no "Z"); JavaScript would otherwise
// parse those as local time and skew every "x ago" label by the UTC offset.
const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export function parseApiTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(HAS_TIMEZONE.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatRelative(date: Date | null, now: number = Date.now()): string {
  if (!date) return "unknown";
  const minutes = Math.max(0, Math.floor((now - date.getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1_440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1_440)}d ago`;
}

export function formatDuration(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 1_440) return `${Math.round(minutes / 60)} h`;
  return `${Math.round(minutes / 1_440)} days`;
}

export function formatAbsoluteUtc(date: Date | null): string {
  if (!date) return "Timestamp unavailable";
  return `${new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }).format(date)} UTC`;
}
