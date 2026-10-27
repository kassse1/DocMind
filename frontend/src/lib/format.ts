export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

// Same language as the rest of the interface (English), in the viewer's time zone.
const dateTime = new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : dateTime.format(date);
}

/** Scores are shown as returned, to a fixed precision, never as percentages. */
export function formatScore(value: number, digits = 3): string {
  return value.toFixed(digits);
}

export function classNames(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

// Any base works: only whether the result stays on it matters.
const LOCAL_BASE = "http://docmind.invalid";

/**
 * A redirect target from the query string, accepted only if it is a path on this origin.
 * Checked the way the browser will resolve it: URL parsing drops tabs and newlines and
 * treats "\" as "/", so "/\t/evil.example" or "/\evil.example" would leave the site.
 */
export function safeNextPath(value: string | null | undefined, fallback = "/dashboard"): string {
  // Control characters and backslashes have no place in our own paths.
  if (!value || !value.startsWith("/") || /[\u0000-\u001f\u007f\\]/.test(value)) return fallback;
  let url: URL;
  try {
    url = new URL(value, LOCAL_BASE);
  } catch {
    return fallback;
  }
  if (url.origin !== LOCAL_BASE) return fallback;
  return `${url.pathname}${url.search}${url.hash}`;
}
