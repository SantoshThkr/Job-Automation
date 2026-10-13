// Typed client for the FastAPI backend (proxied through Next at /api).

export type Mode = "dry_run" | "review" | "auto";
export type Portal = "naukri" | "linkedin" | "instahyre";

export interface Stats {
  jobs_discovered: number;
  relevant_jobs: number;
  applications: number;
  skipped: number;
  failed: number;
  manual_action_required: number;
  dry_run: number;
  already_applied: number;
  queued: number;
  expired: number;
  review_pending: number;
  needs_you: number;
  today_applications: number;
  weekly_applications: number;
  by_status: Record<string, number>;
  by_portal: Record<string, number>;
}

export interface AgentState {
  desired_state: "RUNNING" | "PAUSED" | "STOPPED";
  actual_state: string;
  alive: boolean;
  mode: Mode;
  portals: Portal[];
  pid: number | null;
  heartbeat_at: string | null;
  started_at: string | null;
  run_id: number | null;
  current_activity: string;
  message: string;
  last_error: string;
  pending_job_id: number | null;
  gate_kind: string | null;
  gate_reason: string;
  gate_opened_at: string | null;
  gate_payload: GatePayload;
  repeat_minutes: number;
  phase: string;
  current_step: string;
  current_url: string;
  current_job: CurrentJob | null;
}

export interface CurrentJob {
  id: number;
  title: string;
  company: string;
  portal: Portal;
  url: string;
  status: string;
  match_score: number | null;
}

export interface GateQuestion {
  id: string;
  question: string;
  type: string;
  options: string[];
  required: boolean;
  suggested?: string;
  note?: string;
}

export interface GatePayload {
  questions?: GateQuestion[];
  url?: string;
  job?: string;
}

export interface RunItem {
  id: number;
  mode: Mode;
  portals: Portal[];
  status: string;
  stats: Record<string, number>;
  error: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobSummary {
  id: number;
  portal: Portal;
  title: string;
  company: string;
  location: string;
  url: string;
  match_score: number | null;
  decision: string | null;
  status: string;
  status_reason: string;
  apply_type: string;
  discovered_at: string;
  updated_at: string;
  resume_id: string | null;
  applied_at: string | null;
  ats_platform: string | null;
  frontend_ratio: number | null;
}

export interface QA {
  id: number;
  question: string;
  field_type: string;
  options: string[];
  required: boolean;
  answer: string | null;
  confidence: number;
  source: string;
  needs_review: boolean;
  note: string;
  category?: string;
  mode?: string;
  sources?: string[];
  verified?: boolean | null;
  job_id?: number;
  job_title?: string;
  job_company?: string;
}

export interface EventItem {
  id: number;
  job_id: number | null;
  ts: string;
  level: string;
  type: string;
  message: string;
  data: Record<string, unknown>;
  screenshot: string | null;
  job_title?: string | null;
  job_company?: string | null;
}

export interface Application {
  id: number;
  status: string;
  mode: string;
  apply_target: string;
  ats_platform: string | null;
  resume_id: string | null;
  resume_path: string | null;
  attempts: number;
  last_step: string;
  submit_attempted: boolean;
  confirmed: boolean;
  confirmation_text: string;
  failure_reason: string;
  manual_action_reason: string;
  review_decision: string | null;
  final_url: string;
  resume_url: string;
  steps: number;
  started_at: string | null;
  finished_at: string | null;
  applied_at: string | null;
  answers: QA[];
}

export interface MatchDetails {
  score?: number;
  rule_score?: number;
  decision?: string;
  breakdown?: Record<string, number>;
  frontend_ratio?: number | null;
  category?: string;
  matched_skills?: string[];
  missing_skills?: string[];
  blockers?: string[];
  flags?: string[];
  reasons?: string[];
  analysis?: Record<string, unknown>;
  ai?: Record<string, unknown> | null;
  resume_id?: string | null;
}

export interface JobDetail extends JobSummary {
  external_id: string;
  experience_text: string;
  salary_text: string;
  posted_text: string;
  description: string;
  skills: string[];
  external_url: string | null;
  search_query: string;
  match_details: MatchDetails;
  evaluated_at: string | null;
  application: Application | null;
  events: EventItem[];
}

export interface ProfileIssue {
  field: string;
  message: string;
  severity: "error" | "warning";
}

export interface ProfileResponse {
  path: string;
  profile: Record<string, any>;
  issues: ProfileIssue[];
  ready_for_submission: boolean;
  load_error: string;
}

export interface Resume {
  id: string;
  label: string;
  path: string;
  absolute_path: string;
  exists: boolean;
  size_bytes: number;
  tags: string[];
  default: boolean;
}

export interface NeedsYouQuestion {
  id: number;
  question: string;
  field_type: string;
  options: string[];
  required: boolean;
  why: string;
  category: string;
  profile_can_answer: boolean;
  missing: string;
  saved_answer: string | null;
  saved_scope: string | null;
  suggestion: string | null;
  suggestion_note: string;
  sensitive: boolean;
}

export interface NeedsYouItem {
  job_id: number;
  company: string;
  title: string;
  portal: string;
  url: string;
  ats: string;
  status: string;
  step: string;
  problem: string;
  stop_kind: string;
  why: string;
  live: boolean;
  resume: "live" | "continue_url" | "verify_first" | "restart";
  resume_note: string;
  submit_attempted: boolean;
  verification: string;
  evidence: string[];
  final_url: string;
  updated_at: string | null;
  questions: NeedsYouQuestion[];
  unanswered: number;
}

export interface NeedsYouResponse {
  items: NeedsYouItem[];
  total: number;
  parked: number;
  by_kind: Record<string, number>;
}

export type AnswerScope = "auto" | "global" | "portal" | "company" | "job";

export interface MemoryItem {
  id: number;
  question: string;
  answer: string;
  times_used: number;
  updated_at: string;
  scope?: string;
  company?: string;
  portal?: string;
  job?: string;
  category?: string;
}

export interface AIStatus {
  enabled: boolean;
  available: boolean;
  model: string;
  model_present: boolean;
  models: string[];
  error: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body instanceof FormData ? init?.headers : { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  stats: () => request<Stats>("/api/stats"),
  agent: () => request<AgentState>("/api/agent"),
  start: (mode: Mode, portals: Portal[]) => post<AgentState>("/api/agent/start", { mode, portals }),
  pause: () => post<AgentState>("/api/agent/pause"),
  resume: () => post<AgentState>("/api/agent/resume"),
  stop: () => post<AgentState>("/api/agent/stop"),
  setMode: (mode: Mode) => request<AgentState>("/api/agent/mode", { method: "PUT", body: JSON.stringify({ mode }) }),
  gate: (response: "approve" | "reject", job_id?: number | null, answers?: Record<string, string>) =>
    post<AgentState>("/api/agent/gate", { response, job_id: job_id ?? null, answers: answers ?? null }),
  runs: (limit = 10) => request<RunItem[]>(`/api/agent/runs?limit=${limit}`),
  jobs: (params: Record<string, string | number | boolean | undefined>) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "" && v !== false) q.set(k, String(v));
    });
    return request<{ items: JobSummary[]; total: number; page: number; page_size: number }>(`/api/jobs?${q}`);
  },
  job: (id: number | string) => request<JobDetail>(`/api/jobs/${id}`),
  jobAction: (id: number, action: string, extra: Record<string, unknown> = {}) =>
    post<JobDetail>(`/api/jobs/${id}/action`, { action, ...extra }),
  reevaluate: (id: number, useAi: boolean) => post<JobDetail>(`/api/jobs/${id}/reevaluate?use_ai=${useAi}`),
  events: (limit = 40) => request<EventItem[]>(`/api/events?limit=${limit}`),
  pendingQuestions: () => request<QA[]>("/api/questions/pending"),
  needsYou: (includeParked = false) =>
    request<NeedsYouResponse>(`/api/needs-you${includeParked ? "?include_parked=true" : ""}`),
  answerNeedsYou: (jobId: number, answers: { question: string; answer: string; scope: AnswerScope }[]) =>
    request<{ live: boolean; requeued: boolean; resume: string; resume_note: string; status: string }>(
      `/api/needs-you/${jobId}/answers`, { method: "POST", body: JSON.stringify({ answers }) }),
  saveAnswer: (question: string, answer: string, job_id?: number | null) =>
    post<MemoryItem & { requeued_jobs?: number[] }>("/api/answers/memory", { question, answer, job_id: job_id ?? null }),
  memory: () => request<MemoryItem[]>("/api/answers/memory"),
  updateMemory: (id: number, answer: string) =>
    request<MemoryItem & { requeued_jobs?: number[] }>(`/api/answers/memory/${id}`, { method: "PUT", body: JSON.stringify({ answer }) }),
  deleteMemory: (id: number) => request<{ ok: boolean }>(`/api/answers/memory/${id}`, { method: "DELETE" }),
  profile: () => request<ProfileResponse>("/api/profile"),
  updateSettings: (body: Record<string, unknown>) =>
    request<{ ok: boolean }>("/api/profile/settings", { method: "PUT", body: JSON.stringify(body) }),
  resumes: () => request<Resume[]>("/api/resumes"),
  uploadResume: (form: FormData) => request<{ ok: boolean }>("/api/resumes", { method: "POST", body: form }),
  aiStatus: () => request<AIStatus>("/api/ai/status"),
};
