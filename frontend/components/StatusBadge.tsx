import { STATUS_LABEL } from "@/lib/format";

const TONE: Record<string, string> = {
  APPLIED: "ok",
  ALREADY_APPLIED: "ok",
  QUEUED: "info",
  APPLYING: "info",
  DRY_RUN: "info",
  AWAITING_APPROVAL: "warn",
  REVIEW_PENDING: "warn",
  MANUAL_ACTION_REQUIRED: "warn",
  FAILED: "err",
  EXPIRED: "",
  PARKED: "",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${TONE[status] ?? ""}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function DecisionBadge({ decision }: { decision: string | null | undefined }) {
  if (!decision) return <span className="muted">—</span>;
  const tone = decision === "APPLY" ? "ok" : decision === "SKIP" ? "" : "warn";
  const label = decision === "MANUAL_REVIEW" ? "Review" : decision === "APPLY" ? "Apply" : "Skip";
  return <span className={`badge ${tone}`}>{label}</span>;
}

export function Score({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const tone = value >= 70 ? "hi" : value >= 45 ? "mid" : "lo";
  return <span className={`score ${tone}`}>{Math.round(value)}</span>;
}
