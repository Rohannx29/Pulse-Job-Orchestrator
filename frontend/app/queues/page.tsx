"use client";

import { Fragment, Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, Project, Queue, QueueStats } from "../../lib/api";
import { useEvents } from "../../lib/useEvents";

interface RetryPolicy {
  id: string;
  queue_id: string;
  name: string;
  strategy: "fixed" | "linear" | "exponential";
  max_retries: number;
  base_delay_seconds: number;
}

function QueuesContent() {

  const router = useRouter();
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string>("");
  const [queues, setQueues] = useState<Queue[]>([]);
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [stats, setStats] = useState<Record<string, QueueStats>>({});
  const [policies, setPolicies] = useState<Record<string, RetryPolicy[]>>({});

  // Inline edit state per queue
  const [editing, setEditing] = useState<string | null>(null);
  const [editConcurrency, setEditConcurrency] = useState(4);
  const [editPriority, setEditPriority] = useState(5);

  // Retry policy form state
  const [policyQueue, setPolicyQueue] = useState<string | null>(null);
  const [polName, setPolName] = useState("default");
  const [polStrategy, setPolStrategy] = useState<"fixed" | "linear" | "exponential">("exponential");
  const [polMaxRetries, setPolMaxRetries] = useState(3);
  const [polBaseDelay, setPolBaseDelay] = useState(10);

  useEffect(() => {
    (async () => {
      const projs = await api<Project[]>("/projects");
      setProjects(projs);
      // Respect ?project= from the Projects page link
      const preselect = searchParams.get("project");
      const initial = preselect ?? (projs[0]?.id || "");
      setProjectId(initial);
      setLoading(false);
    })();
  }, [searchParams]);

  async function loadQueues(pid: string) {
    if (!pid) return;
    setQueues(await api<Queue[]>(`/projects/${pid}/queues`));
  }

  useEffect(() => {
    if (projectId) loadQueues(projectId);
  }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Refresh queue list instantly on real-time queue events
  useEvents((event) => {
    if (event.event.startsWith("queue.")) {
      if (projectId) loadQueues(projectId);
    }
  });

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
    const ps = await api<RetryPolicy[]>(`/projects/${projectId}/queues/${queueId}/retry-policies`);
    setPolicies((prev) => ({ ...prev, [queueId]: ps }));
  }

  function startEdit(q: Queue) {
    setEditing(q.id);
    setEditConcurrency(q.concurrency_limit);
    setEditPriority(q.priority);
  }

  async function saveEdit(q: Queue) {
    await api(`/projects/${projectId}/queues/${q.id}`, {
      method: "PATCH",
      body: JSON.stringify({ concurrency_limit: editConcurrency, priority: editPriority }),
    });
    setEditing(null);
    loadQueues(projectId);
  }

  async function createPolicy(queueId: string) {
    await api(`/projects/${projectId}/queues/${queueId}/retry-policies`, {
      method: "POST",
      body: JSON.stringify({
        name: polName,
        strategy: polStrategy,
        max_retries: polMaxRetries,
        base_delay_seconds: polBaseDelay,
      }),
    });
    setPolicyQueue(null);
    const ps = await api<RetryPolicy[]>(`/projects/${projectId}/queues/${queueId}/retry-policies`);
    setPolicies((prev) => ({ ...prev, [queueId]: ps }));
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;

  return (
    <>
      <h1 className="page-title">Queues</h1>
      <p className="page-subtitle">Pause intake, tune concurrency and priority, manage retry policies.</p>

      <div className="toolbar">
        <select className="select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <form onSubmit={createQueue} style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
          <input
            className="input"
            placeholder="new-queue-name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button className="btn" type="submit">+ Create queue</button>
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
                  {/* Name / expand stats */}
                  <td>
                    <button
                      onClick={() => toggleStats(q.id)}
                      style={{ background: "none", border: "none", color: "var(--signal-blue)", cursor: "pointer", fontFamily: "inherit", fontSize: "inherit", padding: 0 }}
                    >
                      {q.name} {expanded === q.id ? "▾" : "▸"}
                    </button>
                  </td>

                  {/* Priority — inline edit */}
                  <td>
                    {editing === q.id ? (
                      <input
                        className="input"
                        type="number"
                        min={1} max={10}
                        value={editPriority}
                        onChange={(e) => setEditPriority(Number(e.target.value))}
                        style={{ width: 60 }}
                      />
                    ) : q.priority}
                  </td>

                  {/* Concurrency — inline edit */}
                  <td>
                    {editing === q.id ? (
                      <input
                        className="input"
                        type="number"
                        min={1}
                        value={editConcurrency}
                        onChange={(e) => setEditConcurrency(Number(e.target.value))}
                        style={{ width: 60 }}
                      />
                    ) : q.concurrency_limit}
                  </td>

                  <td>
                    <span className={`badge ${q.is_paused ? "dead_letter" : "completed"}`}>
                      {q.is_paused ? "paused" : "active"}
                    </span>
                  </td>

                  <td style={{ display: "flex", gap: 6 }}>
                    {editing === q.id ? (
                      <>
                        <button className="btn" onClick={() => saveEdit(q)}>Save</button>
                        <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
                      </>
                    ) : (
                      <button className="btn" onClick={() => startEdit(q)}>Edit</button>
                    )}
                    <button className="btn" onClick={() => toggle(q)}>
                      {q.is_paused ? "Resume" : "Pause"}
                    </button>
                  </td>
                </tr>

                {/* Expanded: stats + retry policies */}
                {expanded === q.id && (
                  <tr>
                    <td colSpan={5} style={{ background: "var(--bg)", padding: 16 }}>
                      {/* Stats */}
                      {!stats[q.id] ? (
                        <span style={{ fontFamily: "var(--font-sans)", color: "var(--text-faint)" }}>Loading stats…</span>
                      ) : (
                        <div className="stat-grid" style={{ margin: "0 0 16px 0" }}>
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

                      {/* Retry Policies */}
                      <p style={{ fontSize: 12, color: "var(--text-dim)", fontWeight: 600, margin: "0 0 8px 0" }}>
                        Retry policies
                      </p>
                      {(policies[q.id] || []).length === 0 && (
                        <p style={{ fontSize: 12, color: "var(--text-faint)", marginBottom: 8 }}>No retry policies defined.</p>
                      )}
                      {(policies[q.id] || []).map((pol) => (
                        <div key={pol.id} style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-dim)", marginBottom: 4 }}>
                          <span style={{ color: "var(--text)" }}>{pol.name}</span>
                          {" · "}{pol.strategy}{" · "}max {pol.max_retries}{" · "}base {pol.base_delay_seconds}s
                        </div>
                      ))}

                      {policyQueue === q.id ? (
                        <div style={{ marginTop: 10, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
                          <input className="input" placeholder="name" value={polName} onChange={(e) => setPolName(e.target.value)} style={{ width: 110 }} />
                          <select className="select" value={polStrategy} onChange={(e) => setPolStrategy(e.target.value as "fixed" | "linear" | "exponential")}>
                            <option value="fixed">fixed</option>
                            <option value="linear">linear</option>
                            <option value="exponential">exponential</option>
                          </select>
                          <input className="input" type="number" min={0} max={20} value={polMaxRetries} onChange={(e) => setPolMaxRetries(Number(e.target.value))} style={{ width: 70 }} placeholder="retries" />
                          <input className="input" type="number" min={1} value={polBaseDelay} onChange={(e) => setPolBaseDelay(Number(e.target.value))} style={{ width: 80 }} placeholder="base(s)" />
                          <button className="btn" onClick={() => createPolicy(q.id)}>Save policy</button>
                          <button className="btn" onClick={() => setPolicyQueue(null)}>Cancel</button>
                        </div>
                      ) : (
                        <button className="btn" style={{ marginTop: 8 }} onClick={() => setPolicyQueue(q.id)}>
                          + Add retry policy
                        </button>
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

export default function QueuesPage() {
  return (
    <Suspense fallback={<p className="page-subtitle">Loading queues...</p>}>
      <QueuesContent />
    </Suspense>
  );
}

