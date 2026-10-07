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
  MANUAL_ACTION_REQUIRED: "Manual action",
  FAILED: "Failed",
  ALREADY_APPLIED: "Already applied",
  DUPLICATE: "Duplicate",
};

export const MODE_LABEL: Record<string, string> = {
  dry_run: "Dry Run",
  review: "Review Mode",
  auto: "Auto Apply",
};
