"use client";

import { useEffect, useState } from "react";
import { api, Worker } from "../../lib/api";

export default function WorkersPage() {
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setWorkers(await api<Worker[]>("/workers"));
    setLoading(false);
  }

  useEffect(() => {
    load();
    const interval = setInterval(load, 10000); // refresh every 10s
    return () => clearInterval(interval);
  }, []);

  if (loading) return <p className="page-subtitle">Loading…</p>;

  const online = workers.filter((w) => w.status === "online").length;

  return (
    <>
      <h1 className="page-title">Workers</h1>
      <p className="page-subtitle">
        Real-time worker pool status. Heartbeats are synced every 15 seconds by Celery Beat.
      </p>

      <div className="stat-grid" style={{ marginBottom: 24 }}>
        <div className={`stat-card ${online > 0 ? "ok" : "warn"}`}>
          <div className="stat-label">Online</div>
          <div className="stat-value">{online}</div>
        </div>
        <div className="stat-card warn">
          <div className="stat-label">Offline</div>
          <div className="stat-value">{workers.length - online}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total registered</div>
          <div className="stat-value">{workers.length}</div>
        </div>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Hostname</th>
              <th>Status</th>
              <th>Last heartbeat</th>
              <th>Registered</th>
            </tr>
          </thead>
          <tbody>
            {workers.length === 0 && (
              <tr>
                <td colSpan={4} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  No workers registered yet. Start a Celery worker to populate this list.
                </td>
              </tr>
            )}
            {workers.map((w) => {
              const isOnline = w.status === "online";
              const heartbeatAge = w.last_heartbeat_at
                ? Math.round((Date.now() - new Date(w.last_heartbeat_at).getTime()) / 1000)
                : null;

              return (
                <tr key={w.id}>
                  <td style={{ fontFamily: "var(--font-mono)" }}>{w.hostname}</td>
                  <td>
                    <span className={`badge ${isOnline ? "completed" : "dead_letter"}`}>
                      {w.status}
                    </span>
                  </td>
                  <td style={{ color: heartbeatAge && heartbeatAge > 60 ? "var(--signal-amber)" : "var(--text)" }}>
                    {w.last_heartbeat_at
                      ? `${heartbeatAge}s ago (${new Date(w.last_heartbeat_at).toLocaleTimeString()})`
                      : "—"}
                  </td>
                  <td>{new Date(w.registered_at).toLocaleString()}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
