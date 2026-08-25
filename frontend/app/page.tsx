"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, DeadLetterJob, hasSession, Project, Queue, Worker } from "../lib/api";

export default function DashboardPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [queues, setQueues] = useState<Queue[]>([]);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [dlq, setDlq] = useState<DeadLetterJob[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      if (!(await hasSession())) {
        router.push("/login");
        return;
      }
      try {
        const projs = await api<Project[]>("/projects");
        setProjects(projs);
        const queueLists = await Promise.all(
          projs.map((p) => api<Queue[]>(`/projects/${p.id}/queues`))
        );
        setQueues(queueLists.flat());
        setWorkers(await api<Worker[]>("/workers"));
        setDlq(await api<DeadLetterJob[]>("/dead-letter-jobs"));
      } finally {
        setLoading(false);
      }
    })();
  }, [router]);

  if (loading) return <p className="page-subtitle">Loading…</p>;

  const workersOnline = workers.filter((w) => w.status === "online").length;

  return (
    <>
      <h1 className="page-title">System overview</h1>
      <p className="page-subtitle">Cross-project health at a glance.</p>

      <div className="stat-grid">
        <div className="stat-card ok">
          <div className="stat-label">Projects</div>
          <div className="stat-value">{projects.length}</div>
        </div>
        <div className="stat-card ok">
          <div className="stat-label">Queues</div>
          <div className="stat-value">{queues.length}</div>
        </div>
        <div className={`stat-card ${workersOnline > 0 ? "ok" : "warn"}`}>
          <div className="stat-label">Workers online</div>
          <div className="stat-value">
            {workersOnline}/{workers.length}
          </div>
        </div>
        <div className={`stat-card ${dlq.length > 0 ? "danger" : "ok"}`}>
          <div className="stat-label">Dead-lettered</div>
          <div className="stat-value">{dlq.length}</div>
        </div>
      </div>

      <h2 style={{ fontSize: 14, color: "var(--text-dim)", fontWeight: 600, marginBottom: 10 }}>
        Dead letter queue — needs attention
      </h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Reason</th>
              <th>Retries at failure</th>
              <th>Failed at</th>
            </tr>
          </thead>
          <tbody>
            {dlq.length === 0 && (
              <tr>
                <td colSpan={4} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  Nothing dead-lettered. Clean run.
                </td>
              </tr>
            )}
            {dlq.map((d) => (
              <tr key={d.id}>
                <td>{d.job_id.slice(0, 8)}</td>
                <td style={{ fontFamily: "var(--font-sans)" }}>{d.reason}</td>
                <td>{d.retry_count_at_failure}</td>
                <td>{new Date(d.failed_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
