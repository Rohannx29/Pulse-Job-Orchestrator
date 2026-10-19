export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let message = `${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) message = data.detail;
    } catch {
      const text = await res.text().catch(() => "");
      if (text) message = text;
    }
    throw new Error(message);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export async function login(email: string, password: string): Promise<void> {
  const res = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username: email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: "Invalid credentials" }));
    throw new Error(body.detail || "Invalid credentials");
  }
}

export async function register(
  email: string,
  password: string,
  fullName: string,
  organizationName: string
): Promise<void> {
  const res = await fetch(`${API_URL}/auth/register`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email,
      password,
      full_name: fullName,
      organization_name: organizationName,
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: "Registration failed" }));
    throw new Error(body.detail || "Registration failed");
  }
}

export async function logout(): Promise<void> {
  await fetch(`${API_URL}/auth/logout`, { method: "POST", credentials: "include" });
}

export async function hasSession(): Promise<boolean> {
  const res = await fetch(`${API_URL}/auth/me`, { credentials: "include" });
  return res.ok;
}

export async function getMe(): Promise<User> {
  return api<User>("/auth/me");
}

export async function acceptInvite(
  token: string,
  email: string,
  password: string,
  fullName?: string
): Promise<void> {
  const res = await fetch(`${API_URL}/auth/accept-invite`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token,
      email,
      password,
      full_name: fullName,
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: "Accepting invite failed" }));
    throw new Error(body.detail || "Accepting invite failed");
  }
}

export interface User {
  id: string;
  email: string;
  full_name?: string | null;
  role: "system_admin" | "org_admin" | "member";
}

export interface Org {
  id: string;
  name: string;
  owner_id: string;
  member_count: number;
  created_at: string;
}

export interface OrgMember {
  id: string;
  user_id: string;
  email: string;
  full_name?: string | null;
  role: "admin" | "member";
  created_at: string;
}

export interface AdminOrg {
  id: string;
  name: string;
  owner_id: string;
  member_count: number;
  created_at: string;
}

export interface AdminUser {
  id: string;
  email: string;
  full_name?: string | null;
  role: "system_admin" | "org_admin" | "member";
  created_at: string;
}

export interface AdminStats {
  total_organizations: number;
  total_users: number;
  total_projects: number;
  total_queues: number;
  total_jobs: number;
}

export interface AuditLog {
  id: string;
  org_id?: string | null;
  user_id?: string | null;
  user_email?: string | null;
  user_name?: string | null;
  action: string;
  resource_type: string;
  resource_id?: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export async function getAuditLogs(params?: {
  action?: string;
  resource_type?: string;
  skip?: number;
  limit?: number;
}): Promise<AuditLog[]> {
  const query = new URLSearchParams();
  if (params?.action) query.set("action", params.action);
  if (params?.resource_type) query.set("resource_type", params.resource_type);
  if (params?.skip) query.set("skip", params.skip.toString());
  if (params?.limit) query.set("limit", params.limit.toString());
  const qs = query.toString();
  return api<AuditLog[]>(`/audit-logs${qs ? `?${qs}` : ""}`);
}


export interface Project {

  id: string;
  name: string;
  description?: string;
  created_at: string;
}

export interface Queue {
  id: string;
  project_id: string;
  name: string;
  priority: number;
  concurrency_limit: number;
  is_paused: boolean;
  created_at: string;
}

export interface Job {
  id: string;
  queue_id: string;
  name: string;
  payload: Record<string, unknown>;
  status: string;
  priority: number;
  run_at?: string | null;
  max_retries: number;
  current_retry_count: number;
  celery_task_id?: string | null;
  depends_on_job_id?: string | null;
  depends_on_job_ids?: string[];
  created_at: string;
  updated_at: string;
}

export interface Worker {
  id: string;
  hostname: string;
  status: string;
  last_heartbeat_at?: string | null;
  registered_at: string;
}

export interface DeadLetterJob {
  id: string;
  job_id: string;
  reason: string;
  retry_count_at_failure: number;
  failed_at: string;
}

export interface QueueStats {
  queue_id: string;
  counts_by_status: Record<string, number>;
  total_jobs: number;
  success_rate: number | null;
  avg_execution_seconds: number | null;
  throughput_last_hour: number;
  current_concurrency_usage: number;
  concurrency_limit: number;
}

export interface JobLog {
  id: string;
  timestamp: string;
  level: string;
  message: string;
}

export interface JobExecution {
  id: string;
  worker_id: string | null;
  attempt_number: number;
  status: string;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
  result: Record<string, unknown> | null;
  logs: JobLog[];
}

export interface JobDetail extends Job {
  claimed_by_worker_id: string | null;
  depends_on_job_id: string | null;
  executions: JobExecution[];
}

export interface ComponentHealth {
  status: "healthy" | "unhealthy";
  latency_ms: number;
  error?: string;
}

export interface SystemHealth {
  status: "healthy" | "unhealthy";
  timestamp: string;
  version: string;
  components: {
    database?: ComponentHealth;
    redis?: ComponentHealth;
  };
}

export async function getSystemHealth(): Promise<SystemHealth> {
  try {
    const res = await fetch(`${API_URL}/health`);
    return await res.json();
  } catch {
    return {
      status: "unhealthy",
      timestamp: new Date().toISOString(),
      version: "0.1.0",
      components: {
        database: { status: "unhealthy", latency_ms: 0, error: "API unreachable" },
        redis: { status: "unhealthy", latency_ms: 0, error: "API unreachable" },
      },
    };
  }
}

// ---------- Webhook Types & API ----------

export interface WebhookSubscription {
  id: string;
  org_id: string;
  url: string;
  secret?: string | null;
  secret_preview: string;
  events: string[];
  is_active: boolean;
  description?: string | null;
  created_at: string;
}

export interface WebhookSubscriptionCreateOut extends WebhookSubscription {
  secret: string;
}

export interface WebhookSecretRotateOut {
  id: string;
  secret: string;
  secret_preview: string;
  previous_secret_valid_until?: string | null;
  message: string;
}

export interface WebhookDelivery {
  id: string;
  subscription_id: string;
  event: string;
  payload: Record<string, unknown>;
  status_code?: number | null;
  response_body?: string | null;
  error_message?: string | null;
  attempt_number: number;
  duration_ms?: number | null;
  is_success: boolean;
  delivered_at: string;
}

export interface WebhookTestResponse {
  status: string;
  delivery_id: string;
  status_code?: number | null;
  is_success: boolean;
  duration_ms?: number | null;
}

export async function getWebhooks(): Promise<WebhookSubscription[]> {
  return api<WebhookSubscription[]>("/orgs/me/webhooks");
}

export async function createWebhook(data: {
  url: string;
  events?: string[];
  description?: string;
}): Promise<WebhookSubscriptionCreateOut> {
  return api<WebhookSubscriptionCreateOut>("/orgs/me/webhooks", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function rotateWebhookSecret(
  id: string,
  grace_period_seconds?: number
): Promise<WebhookSecretRotateOut> {
  return api<WebhookSecretRotateOut>(`/orgs/me/webhooks/${id}/rotate-secret`, {
    method: "POST",
    body: JSON.stringify({ grace_period_seconds: grace_period_seconds ?? 3600 }),
  });
}

export async function updateWebhook(
  id: string,
  data: {
    url?: string;
    events?: string[];
    is_active?: boolean;
    description?: string;
  }
): Promise<WebhookSubscription> {
  return api<WebhookSubscription>(`/orgs/me/webhooks/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteWebhook(id: string): Promise<{ status: string; id: string }> {
  return api<{ status: string; id: string }>(`/orgs/me/webhooks/${id}`, {
    method: "DELETE",
  });
}

export async function testWebhook(id: string): Promise<WebhookTestResponse> {
  return api<WebhookTestResponse>(`/orgs/me/webhooks/${id}/test`, {
    method: "POST",
  });
}

export async function getWebhookDeliveries(id: string, limit = 50): Promise<WebhookDelivery[]> {
  return api<WebhookDelivery[]>(`/orgs/me/webhooks/${id}/deliveries?limit=${limit}`);
}

// ---------- DAG Workflows ----------

export interface DAGNode {
  id: string;
  name: string;
  queue_id: string;
  queue_name?: string;
  status: string;
  priority: number;
  current_retry_count: number;
  max_retries: number;
  parent_job_ids: string[];
  run_at?: string | null;
  created_at: string;
}

export interface DAGEdge {
  parent_id: string;
  child_id: string;
}

export interface DAGGraph {
  root_job_id?: string | null;
  nodes: DAGNode[];
  edges: DAGEdge[];
  is_acyclic: boolean;
}

export async function getJobDag(jobId: string): Promise<DAGGraph> {
  return api<DAGGraph>(`/jobs/${jobId}/dag`);
}

export async function getProjectDag(projectId: string): Promise<DAGGraph> {
  return api<DAGGraph>(`/projects/${projectId}/dag`);
}

