"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, hasSession, Job, Project, Queue } from "../../lib/api";

const STATUSES = ["all", "queued", "scheduled", "claimed", "running", "completed", "failed", "dead_letter"];

export default function JobsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [queues, setQueues] = useState<Queue[]>([]);
  const [queueId, setQueueId] = useState("");
  const [status, setStatus] = useState("all");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [name, setName] = useState("");
  const [failRate, setFailRate] = useState("0");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      if (!(await hasSession())) {
        router.push("/login");
        return;
      }
      const projs = await api<Project[]>("/projects");
      setProjects(projs);
      if (projs.length) setProjectId(projs[0].id);
      setLoading(false);
    })();
  }, [router]);

  useEffect(() => {
    if (!projectId) return;
    api<Queue[]>(`/projects/${projectId}/queues`).then((qs) => {
      setQueues(qs);
      if (qs.length) setQueueId(qs[0].id);
      else setQueueId("");
    });
  }, [projectId]);

  async function loadJobs() {
    if (!projectId || !queueId) return;
    const qs = status === "all" ? "" : `?status=${status}`;
    setJobs(await api<Job[]>(`/projects/${projectId}/queues/${queueId}/jobs${qs}`));
  }

  useEffect(() => {
    loadJobs();
    const interval = setInterval(loadJobs, 4000); // light polling for live status
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, queueId, status]);

  async function createJob(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !queueId) return;
    await api(`/projects/${projectId}/queues/${queueId}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        name,
        payload: { duration_seconds: 2, fail_rate: parseFloat(failRate) || 0 },
        priority: 5,
        max_retries: 3,
      }),
    });
    setName("");
    loadJobs();
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

      <div className="toolbar">
        <select className="select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select className="select" value={queueId} onChange={(e) => setQueueId(e.target.value)}>
          {queues.map((q) => (
            <option key={q.id} value={q.id}>
              {q.name}
            </option>
          ))}
        </select>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      <form onSubmit={createJob} className="toolbar">
        <input
          className="input"
          placeholder="job name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          className="input"
          style={{ width: 90 }}
          title="fail_rate (0-1), useful for testing retry/DLQ behavior"
          value={failRate}
          onChange={(e) => setFailRate(e.target.value)}
        />
        <button className="btn" type="submit">
          + Queue job
        </button>
      </form>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Retries</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 && (
              <tr>
                <td colSpan={5} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
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
                </td>
                <td>
                  <span className={`badge ${j.status}`}>{j.status}</span>
                </td>
                <td>
                  {j.current_retry_count}/{j.max_retries}
                </td>
                <td>{new Date(j.created_at).toLocaleTimeString()}</td>
                <td>
                  {(j.status === "failed" || j.status === "dead_letter") && (
                    <button className="btn" onClick={() => retry(j.id)}>
                      Retry
                    </button>
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
