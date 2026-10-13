export function fmtDate(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, withTime
    ? { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "short", year: "numeric" });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const PORTAL_LABEL: Record<string, string> = {
  naukri: "Naukri",
  linkedin: "LinkedIn",
  instahyre: "Instahyre",
};

export const STATUS_LABEL: Record<string, string> = {
  NEW: "New",
  SKIPPED: "Skipped",
  REVIEW_PENDING: "Needs decision",
  QUEUED: "Queued",
  APPLYING: "Applying",
  AWAITING_APPROVAL: "Awaiting approval",
  APPLIED: "Applied",
  DRY_RUN: "Dry run",
  MANUAL_ACTION_REQUIRED: "Needs you",
  FAILED: "Failed",
  ALREADY_APPLIED: "Already applied",
  DUPLICATE: "Duplicate",
  EXPIRED: "Closed / expired",
  PARKED: "Parked",
};

export function hostOf(url: string | null | undefined): string {
  if (!url) return "";
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export const MODE_LABEL: Record<string, string> = {
  dry_run: "Dry Run",
  review: "Review Mode",
  auto: "Auto Apply",
};

export const ANSWER_MODE: Record<string, string> = {
  profile: "Profile fact",
  derived: "Derived",
  ai_generated: "AI generated",
  composed: "Written from profile",
  memory: "Your saved answer",
  ai_derived: "AI derived",
  custom: "Your custom answer",
  user: "Answered by you",
  ask: "Needs you",
  skipped: "Left blank",
  not_a_question: "Not a question",
};

/** "profile + job description" from source keys like professional.work_history[1], job.description */
export function sourceSummary(sources: string[] | undefined): string {
  if (!sources?.length) return "";
  const parts: string[] = [];
  if (sources.some((s) => !s.startsWith("job.") && s !== "page" && s !== "your saved answer" && s !== "you")) parts.push("profile");
  if (sources.some((s) => s.startsWith("job."))) parts.push("job description");
  if (sources.includes("your saved answer")) parts.push("your saved answer");
  return parts.join(" + ");
}

/**
 * Distinct, non-empty strings in first-seen order. A list of plain strings (a question's choices, skills, reasons)
 * is keyed by its own text when rendered, so every entry must occur once: two identical choices cannot be told
 * apart anyway (an answer is matched by its text).
 */
export function uniqueStrings(values: readonly (string | null | undefined)[] | null | undefined): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values ?? []) {
    const text = (v ?? "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

/**
 * Records by their real identifier, first occurrence kept. `duplicates` names ids that were sent more than once -
 * the same record twice, which is a server bug to report, never two records to show.
 */
export function uniqueById<T>(records: readonly T[], idOf: (record: T) => string | number): { items: T[]; duplicates: (string | number)[] } {
  const seen = new Set<string | number>();
  const items: T[] = [];
  const duplicates: (string | number)[] = [];
  for (const r of records) {
    const id = idOf(r);
    if (seen.has(id)) {
      if (!duplicates.includes(id)) duplicates.push(id);
      continue;
    }
    seen.add(id);
    items.push(r);
  }
  return { items, duplicates };
}

/**
 * A link target taken from scraped data (job pages, company sites) is only usable when it is a web address:
 * http(s), or a path on this dashboard. Anything else (javascript:, data:, file:...) yields undefined, which
 * renders the link inert.
 */
export function safeHref(url: string | null | undefined): string | undefined {
  const raw = (url ?? "").trim();
  if (!raw) return undefined;
  if (raw.startsWith("/") && !raw.startsWith("//")) return raw;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}
