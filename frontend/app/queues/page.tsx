"use client";

import { Fragment, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, hasSession, Project, Queue, QueueStats } from "../../lib/api";

export default function QueuesPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string>("");
  const [queues, setQueues] = useState<Queue[]>([]);
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [stats, setStats] = useState<Record<string, QueueStats>>({});

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

  async function loadQueues(pid: string) {
    if (!pid) return;
    setQueues(await api<Queue[]>(`/projects/${pid}/queues`));
  }

  useEffect(() => {
    if (projectId) loadQueues(projectId);
  }, [projectId]);

  async function toggle(q: Queue) {
    const action = q.is_paused ? "resume" : "pause";
    await api(`/projects/${projectId}/queues/${q.id}/${action}`, { method: "POST" });
    loadQueues(projectId);
  }

  async function createQueue(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    await api(`/projects/${projectId}/queues`, {
      method: "POST",
      body: JSON.stringify({ name: newName, priority: 5, concurrency_limit: 4 }),
    });
    setNewName("");
    loadQueues(projectId);
  }

  async function toggleStats(queueId: string) {
    if (expanded === queueId) {
      setExpanded(null);
      return;
    }
    setExpanded(queueId);
    const s = await api<QueueStats>(`/projects/${projectId}/queues/${queueId}/stats`);
    setStats((prev) => ({ ...prev, [queueId]: s }));
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;

  return (
    <>
      <h1 className="page-title">Queues</h1>
      <p className="page-subtitle">Pause intake, tune concurrency, and manage priority per queue.</p>

      <div className="toolbar">
        <select className="select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <form onSubmit={createQueue} style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
          <input
            className="input"
            placeholder="new-queue-name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button className="btn" type="submit">
            + Create queue
          </button>
        </form>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Priority</th>
              <th>Concurrency</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {queues.length === 0 && (
              <tr>
                <td colSpan={5} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  No queues in this project yet.
                </td>
              </tr>
            )}
            {queues.map((q) => (
              <Fragment key={q.id}>
                <tr>
                  <td>
                    <button
                      onClick={() => toggleStats(q.id)}
                      style={{ background: "none", border: "none", color: "var(--signal-blue)", cursor: "pointer", fontFamily: "inherit", fontSize: "inherit", padding: 0 }}
                    >
                      {q.name} {expanded === q.id ? "▾" : "▸"}
                    </button>
                  </td>
                  <td>{q.priority}</td>
                  <td>{q.concurrency_limit}</td>
                  <td>
                    <span className={`badge ${q.is_paused ? "dead_letter" : "completed"}`}>
                      {q.is_paused ? "paused" : "active"}
                    </span>
                  </td>
                  <td>
                    <button className="btn" onClick={() => toggle(q)}>
                      {q.is_paused ? "Resume" : "Pause"}
                    </button>
                  </td>
                </tr>
                {expanded === q.id && (
                  <tr>
                    <td colSpan={5} style={{ background: "var(--bg)" }}>
                      {!stats[q.id] ? (
                        <span style={{ fontFamily: "var(--font-sans)", color: "var(--text-faint)" }}>Loading stats…</span>
                      ) : (
                        <div className="stat-grid" style={{ margin: "8px 0" }}>
                          <div className="stat-card ok">
                            <div className="stat-label">Total jobs</div>
                            <div className="stat-value">{stats[q.id].total_jobs}</div>
                          </div>
                          <div className={`stat-card ${stats[q.id].success_rate === null ? "warn" : stats[q.id].success_rate! >= 0.8 ? "ok" : "warn"}`}>
                            <div className="stat-label">Success rate</div>
                            <div className="stat-value">
                              {stats[q.id].success_rate === null ? "—" : `${Math.round(stats[q.id].success_rate! * 100)}%`}
                            </div>
                          </div>
                          <div className="stat-card ok">
                            <div className="stat-label">Avg exec time</div>
                            <div className="stat-value">
                              {stats[q.id].avg_execution_seconds === null ? "—" : `${stats[q.id].avg_execution_seconds!.toFixed(1)}s`}
                            </div>
                          </div>
                          <div className="stat-card ok">
                            <div className="stat-label">Throughput (1h)</div>
                            <div className="stat-value">{stats[q.id].throughput_last_hour}</div>
                          </div>
                          <div className={`stat-card ${stats[q.id].current_concurrency_usage >= stats[q.id].concurrency_limit ? "warn" : "ok"}`}>
                            <div className="stat-label">Concurrency</div>
                            <div className="stat-value">
                              {stats[q.id].current_concurrency_usage}/{stats[q.id].concurrency_limit}
                            </div>
                          </div>
                          {Object.entries(stats[q.id].counts_by_status).map(([status, count]) => (
                            <div key={status} className="stat-card">
                              <div className="stat-label">{status}</div>
                              <div className="stat-value">{count}</div>
                            </div>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
