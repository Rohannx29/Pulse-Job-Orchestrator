"use client";

import { useEffect, useState } from "react";
import { api, Project, Queue } from "../../lib/api";

interface Schedule {
  id: string;
  queue_id: string;
  name: string;
  cron_expression: string;
  payload_template: Record<string, unknown>;
  is_active: boolean;
  created_at: string;
}

export default function SchedulesPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [queues, setQueues] = useState<Queue[]>([]);
  const [queueId, setQueueId] = useState("");
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Form fields
  const [schName, setSchName] = useState("");
  const [cron, setCron] = useState("*/5 * * * *");
  const [payloadRaw, setPayloadRaw] = useState('{"type":"simulate","duration_seconds":2,"fail_rate":0}');
  const [payloadError, setPayloadError] = useState<string | null>(null);

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
      if (qs.length) setQueueId(qs[0].id);
      else setQueueId("");
    });
  }, [projectId]);

  async function loadSchedules() {
    if (!projectId || !queueId) return;
    setSchedules(await api<Schedule[]>(`/projects/${projectId}/queues/${queueId}/schedules`));
  }

  useEffect(() => { loadSchedules(); }, [projectId, queueId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function createSchedule(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setPayloadError(null);

    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(payloadRaw);
    } catch {
      setPayloadError("Payload template must be valid JSON.");
      return;
    }

    try {
      await api(`/projects/${projectId}/queues/${queueId}/schedules`, {
        method: "POST",
        body: JSON.stringify({ name: schName, cron_expression: cron, payload_template: payload }),
      });
      setSchName("");
      setCron("*/5 * * * *");
      setPayloadRaw('{"type":"simulate","duration_seconds":2,"fail_rate":0}');
      loadSchedules();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create schedule.");
    }
  }

  async function toggleSchedule(id: string) {
    await api(`/schedules/${id}/toggle`, { method: "POST" });
    loadSchedules();
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;

  return (
    <>
      <h1 className="page-title">Schedules</h1>
      <p className="page-subtitle">Create and manage recurring cron jobs. Beat evaluates active schedules and creates jobs on each tick.</p>

      {/* Project / Queue selectors */}
      <div className="toolbar">
        <select className="select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select className="select" value={queueId} onChange={(e) => setQueueId(e.target.value)}>
          {queues.map((q) => <option key={q.id} value={q.id}>{q.name}</option>)}
        </select>
      </div>

      {/* Create schedule form */}
      <div className="table-wrap" style={{ padding: 16, marginBottom: 20 }}>
        <p style={{ fontSize: 12, color: "var(--text-dim)", margin: "0 0 12px 0", fontWeight: 600 }}>
          New schedule
        </p>
        <form onSubmit={createSchedule}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
            <div className="field" style={{ margin: 0 }}>
              <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Name</label>
              <input
                className="input"
                placeholder="nightly-cleanup"
                value={schName}
                onChange={(e) => setSchName(e.target.value)}
                required
              />
            </div>
            <div className="field" style={{ margin: 0 }}>
              <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Cron expression (5 fields)</label>
              <input
                className="input"
                placeholder="*/5 * * * *"
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                required
                style={{ fontFamily: "var(--font-mono)" }}
              />
            </div>
          </div>
          <div className="field" style={{ margin: "0 0 10px 0" }}>
            <label style={{ fontSize: 12, color: "var(--text-dim)" }}>Payload template (JSON)</label>
            <textarea
              className="input"
              rows={3}
              value={payloadRaw}
              onChange={(e) => setPayloadRaw(e.target.value)}
              style={{ fontFamily: "var(--font-mono)", fontSize: 12, resize: "vertical", width: "100%" }}
            />
            {payloadError && (
              <span style={{ color: "var(--signal-red)", fontSize: 12 }}>{payloadError}</span>
            )}
          </div>
          {error && <p style={{ color: "var(--signal-red)", fontSize: 12, marginBottom: 10 }}>{error}</p>}
          <button className="btn" type="submit">+ Create schedule</button>
        </form>
      </div>

      {/* Schedules table */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Cron</th>
              <th>Status</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {schedules.length === 0 && (
              <tr>
                <td colSpan={5} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  No schedules for this queue yet.
                </td>
              </tr>
            )}
            {schedules.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td style={{ fontFamily: "var(--font-mono)" }}>{s.cron_expression}</td>
                <td>
                  <span className={`badge ${s.is_active ? "completed" : "dead_letter"}`}>
                    {s.is_active ? "active" : "inactive"}
                  </span>
                </td>
                <td>{new Date(s.created_at).toLocaleDateString()}</td>
                <td>
                  <button className="btn" onClick={() => toggleSchedule(s.id)}>
                    {s.is_active ? "Deactivate" : "Activate"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
