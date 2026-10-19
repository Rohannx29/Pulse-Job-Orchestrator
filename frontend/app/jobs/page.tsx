"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Job, Project, Queue } from "../../lib/api";
import { useEvents } from "../../lib/useEvents";

const STATUSES = ["all", "blocked", "queued", "scheduled", "claimed", "running", "completed", "failed", "dead_letter"];

type JobType = "simulate" | "http_request";

export default function JobsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [queues, setQueues] = useState<Queue[]>([]);
  const [queueId, setQueueId] = useState("");
  const [status, setStatus] = useState("all");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);

  // ---- Job creation form ----
  const [showForm, setShowForm] = useState(false);
  const [jobName, setJobName] = useState("");
  const [jobType, setJobType] = useState<JobType>("simulate");
  const [priority, setPriority] = useState(5);
  const [maxRetries, setMaxRetries] = useState(3);
  const [runAt, setRunAt] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [dependsOnId, setDependsOnId] = useState("");
  // simulate fields
  const [duration, setDuration] = useState(2);
  const [failRate, setFailRate] = useState(0);
  // http_request fields
  const [httpUrl, setHttpUrl] = useState("");
  const [httpMethod, setHttpMethod] = useState("GET");
  const [httpBodyRaw, setHttpBodyRaw] = useState("{}");

  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const projs = await api<Project[]>("/projects");
      setProjects(projs);
      if (projs.length) setProjectId(projs[0].id);
      setLoading(false);
    })();
  }, []);

  useEffect(() => {
    if (!projectId) return;
    api<Queue[]>(`/projects/${projectId}/queues`).then((qs) => {
      setQueues(qs);
      setQueueId(qs[0]?.id || "");
    });
  }, [projectId]);

  async function loadJobs() {
    if (!projectId || !queueId) return;
    const qs = status === "all" ? "" : `?status=${status}`;
    setJobs(await api<Job[]>(`/projects/${projectId}/queues/${queueId}/jobs${qs}`));
  }

  useEffect(() => {
    loadJobs();
    // Relaxed background interval (15s) as a backstop while SSE handles real-time updates
    const interval = setInterval(loadJobs, 15000);
    return () => clearInterval(interval);
  }, [projectId, queueId, status]); // eslint-disable-line react-hooks/exhaustive-deps

  // Real-time SSE updates: update job status inline instantly
  useEvents((event) => {
    if (event.event.startsWith("job.")) {
      if (event.data?.job_id && event.data?.status) {
        setJobs((prev) =>
          prev.map((j) =>
            j.id === event.data.job_id
              ? { ...j, status: event.data.status }
              : j
          )
        );
      }
      // Re-fetch to ensure ordering and fresh retry counts are synced
      loadJobs();
    }
  });

  function buildPayload() {
    if (jobType === "simulate") {
      return { type: "simulate", duration_seconds: duration, fail_rate: failRate };
    }
    // http_request
    let body: unknown = undefined;
    try { body = JSON.parse(httpBodyRaw); } catch { /* leave as undefined */ }
    return { type: "http_request", url: httpUrl, method: httpMethod, body };
  }

  async function createJob(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!jobName.trim() || !queueId) return;
    const parentIds = dependsOnId
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);

    try {
      await api(`/projects/${projectId}/queues/${queueId}/jobs`, {
        method: "POST",
        body: JSON.stringify({
          name: jobName,
          payload: buildPayload(),
          priority,
          max_retries: maxRetries,
          run_at: runAt || undefined,
          idempotency_key: idempotencyKey || undefined,
          depends_on_job_ids: parentIds.length > 0 ? parentIds : undefined,
        }),
      });
      // Reset form
      setJobName("");
      setRunAt("");
      setIdempotencyKey("");
      setDependsOnId("");
      setHttpUrl("");
      setHttpBodyRaw("{}");
      setShowForm(false);
      loadJobs();
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Failed to create job.");
    }
  }

  async function retry(jobId: string) {
    await api(`/jobs/${jobId}/retry`, { method: "POST" });
    loadJobs();
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;

  return (
    <>
      <h1 className="page-title">Job explorer</h1>
      <p className="page-subtitle">Live status, retries and manual re-queue.</p>

      {/* Filters toolbar */}
      <div className="toolbar">
        <select className="select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select className="select" value={queueId} onChange={(e) => setQueueId(e.target.value)}>
          {queues.map((q) => <option key={q.id} value={q.id}>{q.name}</option>)}
        </select>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button className="btn" style={{ marginLeft: "auto" }} onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "+ Queue job"}
        </button>
      </div>

      {/* Expanded job creation form */}
      {showForm && (
        <div className="table-wrap" style={{ padding: 16, marginBottom: 20 }}>
          <p style={{ fontSize: 12, color: "var(--text-dim)", margin: "0 0 14px 0", fontWeight: 600 }}>New job</p>
          <form onSubmit={createJob}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginBottom: 10 }}>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Name *</label>
                <input className="input" value={jobName} onChange={(e) => setJobName(e.target.value)} required placeholder="my-job" />
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Job type</label>
                <select className="select" value={jobType} onChange={(e) => setJobType(e.target.value as JobType)}>
                  <option value="simulate">simulate</option>
                  <option value="http_request">http_request</option>
                </select>
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Priority (1=highest, 10=lowest)</label>
                <input className="input" type="number" min={1} max={10} value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
              </div>
            </div>

            {/* Type-specific fields */}
            {jobType === "simulate" && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
                <div className="field" style={{ margin: 0 }}>
                  <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Duration (seconds)</label>
                  <input className="input" type="number" min={0} max={30} value={duration} onChange={(e) => setDuration(Number(e.target.value))} />
                </div>
                <div className="field" style={{ margin: 0 }}>
                  <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Fail rate (0–1, 1=always fails)</label>
                  <input className="input" type="number" min={0} max={1} step={0.1} value={failRate} onChange={(e) => setFailRate(Number(e.target.value))} />
                </div>
              </div>
            )}
            {jobType === "http_request" && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr auto 1fr", gap: 10, marginBottom: 10 }}>
                <div className="field" style={{ margin: 0 }}>
                  <label style={{ fontSize: 12, color: "var(--text-dim)" }}>URL *</label>
                  <input className="input" type="url" value={httpUrl} onChange={(e) => setHttpUrl(e.target.value)} placeholder="https://example.com/webhook" required />
                </div>
                <div className="field" style={{ margin: 0 }}>
                  <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Method</label>
                  <select className="select" value={httpMethod} onChange={(e) => setHttpMethod(e.target.value)}>
                    {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m}>{m}</option>)}
                  </select>
                </div>
                <div className="field" style={{ margin: 0 }}>
                  <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Body (JSON)</label>
                  <input className="input" value={httpBodyRaw} onChange={(e) => setHttpBodyRaw(e.target.value)} placeholder="{}" style={{ fontFamily: "var(--font-mono)" }} />
                </div>
              </div>
            )}

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 10, marginBottom: 10 }}>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Max retries</label>
                <input className="input" type="number" min={0} max={20} value={maxRetries} onChange={(e) => setMaxRetries(Number(e.target.value))} />
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Run at (delayed, optional)</label>
                <input className="input" type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} />
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Idempotency key (optional)</label>
                <input className="input" value={idempotencyKey} onChange={(e) => setIdempotencyKey(e.target.value)} placeholder="unique-key" />
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Prerequisites (comma-separated for DAG)</label>
                <input className="input" value={dependsOnId} onChange={(e) => setDependsOnId(e.target.value)} placeholder="uuid-1, uuid-2" style={{ fontFamily: "var(--font-mono)" }} />
              </div>
            </div>

            {formError && <p style={{ color: "var(--signal-red)", fontSize: 12, marginBottom: 8 }}>{formError}</p>}
            <button className="btn" type="submit">Submit job</button>
          </form>
        </div>
      )}

      {/* Jobs table */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Priority</th>
              <th>Retries</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 && (
              <tr>
                <td colSpan={6} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  No jobs match this filter.
                </td>
              </tr>
            )}
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>
                  <Link href={`/jobs/${j.id}`} style={{ color: "var(--signal-blue)" }}>
                    {j.name}
                  </Link>
                  {((j.depends_on_job_ids && j.depends_on_job_ids.length > 0) || j.depends_on_job_id) && (
                    <span className="badge queued" style={{ fontSize: 10, padding: "1px 5px", marginLeft: 6 }}>
                      DAG ({j.depends_on_job_ids?.length || 1})
                    </span>
                  )}
                </td>
                <td><span className={`badge ${j.status}`}>{j.status}</span></td>
                <td>{j.priority}</td>
                <td>{j.current_retry_count}/{j.max_retries}</td>
                <td>{new Date(j.created_at).toLocaleTimeString()}</td>
                <td>
                  {(j.status === "failed" || j.status === "dead_letter") && (
                    <button className="btn" onClick={() => retry(j.id)}>Retry</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
