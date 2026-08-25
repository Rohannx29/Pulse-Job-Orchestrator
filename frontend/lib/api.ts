const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
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
  if (!res.ok) throw new Error("Invalid credentials");
}

export async function logout(): Promise<void> {
  await fetch(`${API_URL}/auth/logout`, { method: "POST", credentials: "include" });
}

export async function hasSession(): Promise<boolean> {
  const res = await fetch(`${API_URL}/auth/me`, { credentials: "include" });
  return res.ok;
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
