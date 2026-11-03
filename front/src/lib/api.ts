export type BrowserCacheEntry = {
  id: string;
  url: string;
  host: string;
  path: string;
  content_type: string;
  status: number;
  size: number;
  scope: "standard" | "more" | "unknown" | string;
  risk_level?: "high" | "medium" | "low" | "unknown" | string;
  risk_reasons?: string[];
  replay_safe?: boolean;
  active: boolean;
  cached_at: string;
  mtime: number;
};

export type BrowserCacheSnapshot = {
  enabled: boolean;
  savings_level: string;
  root: string;
  total_bytes: number;
  max_total_bytes: number;
  max_entry_bytes: number;
  entry_count: number;
  active_count: number;
  active_bytes: number;
  high_risk_count?: number;
  medium_risk_count?: number;
  high_risk_bytes?: number;
  refills_on_miss: boolean;
  deleted_files?: number;
  errors?: number;
  entries: BrowserCacheEntry[];
};

export type JobStatus = {
  running: boolean;
  started_at?: number | null;
  finished_at?: number | null;
  target_count: number;
  workers: number;
  source: string;
  last_error?: string;
  log_count: number;
  latest_log_id: number;
  completed_count: number;
  success_count: number;
  failure_count: number;
  progress_percent: number;
  current_stage: string;
  current_email: string;
  batch_id?: string;
};

export type AccountRecord = {
  id: number;
  email: string;
  password: string;
  status: string;
  success: boolean;
  provider: string;
  platform: string;
  email_account_id: string;
  email_disable_status: string;
  email_disabled_at: string;
  email_disable_error: string;
  account_file: string;
  failure_type: string;
  failure_reason: string;
  screenshot_path: string;
  screenshot_url: string;
  exception_traceback: string;
  exception_type: string;
  has_exception_traceback: boolean;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  batch_id: string;
  source: string;
  worker_id: number;
  extra?: Record<string, unknown>;
  exit_ip?: string;
  exit_ip_at_start?: string;
};

export type FlaggedExitIp = {
  ip: string;
  first_seen_at: string;
  last_seen_at: string;
  hit_count: number;
  last_email: string;
  last_risk_source: string;
  last_failure_reason: string;
};

export type Stats = {
  total: number;
  success: number;
  failure: number;
  skipped: number;
  cancelled: number;
  email_disabled: number;
  email_disable_failed: number;
  today_total: number;
  today_success: number;
  unique_success_emails: number;
  avg_success_seconds: number;
  providers?: Array<{ provider: string; total: number; success: number }>;
};

export type LogItem = {
  id: number;
  time: string;
  message: string;
};

export type AuthState = {
  enabled: boolean;
  setup_required?: boolean;
  authenticated: boolean;
  username: string;
};

export type UpdateStatus = "unchecked" | "up_to_date" | "update_available" | "check_failed";

export type VersionInfo = {
  currentVersion: string;
  latestVersion: string;
  updateAvailable: boolean;
  status: UpdateStatus;
  checkedAt: string | null;
  releaseUrl: string;
  releaseNotes: string;
  error: string;
};

export type OutlookEmailGroup = {
  id: number;
  name: string;
  account_count: number;
  is_system: boolean;
};

export type ConfigFileSnapshot = {
  path: string;
  exists: boolean;
  size: number;
  modified_at: string;
  content: string;
  parse_error: string;
  sensitive_keys: string[];
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    ...init,
  });
  let data: any = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok || data?.ok === false) {
    if (response.status === 401 && data?.auth_required) {
      window.dispatchEvent(
        new CustomEvent("x-register-auth-required", { detail: { setupRequired: !!data?.setup_required } })
      );
    }
    const detail = data?.detail;
    const detailText = Array.isArray(detail)
      ? detail.map((item: any) => item?.msg || JSON.stringify(item)).join("; ")
      : detail;
    throw new Error(data?.error || detailText || `请求失败 (${response.status})`);
  }
  return data as T;
}

export const api = {
  health: () => request<{ ok: boolean; version?: string }>("/api/health"),
  versionInfo: () => request<{ ok: boolean; version: VersionInfo }>("/api/system/version"),
  checkForUpdates: () =>
    request<{ ok: boolean; version: VersionInfo }>("/api/system/update/check", { method: "POST" }),
  authMe: () => request<{ ok: boolean } & AuthState>("/api/auth/me"),
  setup: (username: string, password: string, confirmPassword: string) =>
    request<{ ok: boolean } & AuthState>("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, confirm_password: confirmPassword }),
    }),
  login: (username: string, password: string) =>
    request<{ ok: boolean } & AuthState>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  stats: () => request<{ ok: boolean; stats: Stats; job: JobStatus; x_account_job: JobStatus }>("/api/stats"),
  accounts: (
    params: {
      status?: string;
      emailDisableStatus?: string;
      q?: string;
      batchId?: string;
      platform?: string;
      limit?: number;
      offset?: number;
    } = {}
  ) => {
    const sp = new URLSearchParams();
    if (params.status) sp.set("status", params.status);
    if (params.emailDisableStatus) sp.set("email_disable_status", params.emailDisableStatus);
    if (params.q) sp.set("q", params.q);
    if (params.batchId) sp.set("batch_id", params.batchId);
    if (params.platform) sp.set("platform", params.platform);
    if (params.limit) sp.set("limit", String(params.limit));
    if (params.offset) sp.set("offset", String(params.offset));
    const qs = sp.toString();
    return request<{
      ok: boolean;
      items: AccountRecord[];
      total: number;
      count: number;
      has_more?: boolean;
      offset: number;
      limit: number;
    }>(`/api/accounts${qs ? `?${qs}` : ""}`);
  },
  account: (id: number) => request<{ ok: boolean; item: AccountRecord }>(`/api/accounts/${id}`),
  deleteAccounts: (ids: number[], deleteFiles = true) =>
    request<{ ok: boolean; deleted: number; deleted_files: number; side_lines: number; file_errors: string[] }>(
      "/api/accounts/delete",
      { method: "POST", body: JSON.stringify({ ids, delete_files: deleteFiles }) }
    ),
  flaggedExitIps: () =>
    request<{ ok: boolean; items: FlaggedExitIp[]; total: number }>("/api/flagged-exit-ips"),
  deleteFlaggedExitIp: (ip: string) =>
    request<{ ok: boolean; deleted: string }>("/api/flagged-exit-ips/delete", {
      method: "POST",
      body: JSON.stringify({ ip }),
    }),
  outlookemailGroups: () =>
    request<{ ok: boolean; groups: OutlookEmailGroup[] }>("/api/outlookemail/groups"),
  getConfig: () => request<{ ok: boolean; config: Record<string, any> }>("/api/config"),
  getConfigFile: () => request<{ ok: boolean; file: ConfigFileSnapshot }>("/api/config/file"),
  saveConfig: (config: Record<string, any>) =>
    request<{ ok: boolean; config: Record<string, any>; changed: string[] }>("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  job: () => request<{ ok: boolean; job: JobStatus }>("/api/job"),
  jobs: () =>
    request<{
      ok: boolean;
      running: boolean;
      x_registration: JobStatus;
      x_account: JobStatus;
    }>("/api/jobs"),
  logs: (afterId = 0, limit = 500) =>
    request<{ ok: boolean; logs: LogItem[]; job: JobStatus }>(
      `/api/job/logs?after_id=${afterId}&limit=${limit}`
    ),
  startJob: (payload: { count?: number; workers?: number; config?: Record<string, any> }) =>
    request<{ ok: boolean; job: JobStatus }>("/api/job/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  stopJob: () => request<{ ok: boolean; job: JobStatus }>("/api/job/stop", { method: "POST" }),
  xAccountJob: () => request<{ ok: boolean; job: JobStatus }>("/api/x-account/job"),
  xAccountLogs: (afterId = 0, limit = 500) =>
    request<{ ok: boolean; logs: LogItem[]; job: JobStatus }>(
      `/api/x-account/job/logs?after_id=${afterId}&limit=${limit}`
    ),
  startXAccountJob: (count = 1) =>
    request<{ ok: boolean; job: JobStatus }>("/api/x-account/job/start", {
      method: "POST",
      body: JSON.stringify({ count, workers: 1 }),
    }),
  stopXAccountJob: () =>
    request<{ ok: boolean; job: JobStatus }>("/api/x-account/job/stop", { method: "POST" }),
  killAllBrowsers: () =>
    request<{ ok: boolean; killed: number; profiles_cleaned: number; job: JobStatus; x_account_job: JobStatus }>(
      "/api/browser/kill-all",
      { method: "POST" }
    ),
  browserCache: () => request<{ ok: boolean } & BrowserCacheSnapshot>("/api/browser/cache"),
  clearBrowserCache: () =>
    request<{ ok: boolean } & BrowserCacheSnapshot>("/api/browser/cache/clear", { method: "POST" }),
  connectivity: () =>
    request<{ ok: boolean; items: Array<{ name: string; ok: boolean; detail: string }>; blocked: boolean }>(
      "/api/connectivity",
      { method: "POST" }
    ),
  xAccountConnectivity: () =>
    request<{ ok: boolean; items: Array<{ name: string; ok: boolean; detail: string }>; blocked: boolean }>(
      "/api/x-account/connectivity",
      { method: "POST" }
    ),
};
