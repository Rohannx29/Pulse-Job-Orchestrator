"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { api, DAGGraph, getJobDag, hasSession, JobDetail } from "../../../lib/api";

export default function JobDetailPage() {
  const params = useParams();
  const router = useRouter();
  const jobId = params.id as string;
  const [job, setJob] = useState<JobDetail | null>(null);
  const [dag, setDag] = useState<DAGGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [jobData, dagData] = await Promise.all([
        api<JobDetail>(`/jobs/${jobId}`),
        getJobDag(jobId).catch(() => null),
      ]);
      setJob(jobData);
      setDag(dagData);
      setError(null);
    } catch {
      setError("Could not load this job.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | undefined;
    (async () => {
      if (!(await hasSession())) {
        router.push("/login");
        return;
      }
      load();
      interval = setInterval(load, 3000);
    })();
    return () => {
      if (interval) clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  async function retry() {
    await api(`/jobs/${jobId}/retry`, { method: "POST" });
    load();
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;
  if (error || !job) return <p className="page-subtitle">{error || "Job not found."}</p>;

  const parentIds = job.depends_on_job_ids && job.depends_on_job_ids.length > 0
    ? job.depends_on_job_ids
    : (job.depends_on_job_id ? [job.depends_on_job_id] : []);

  return (
    <>
      <button className="btn" onClick={() => router.back()} style={{ marginBottom: 16 }}>
        ← Back
      </button>
      <h1 className="page-title">{job.name}</h1>
      <p className="page-subtitle">
        <span className={`badge ${job.status}`}>{job.status}</span>
        {"  ·  "}created {new Date(job.created_at).toLocaleString()}
      </p>

      <div className="stat-grid" style={{ marginBottom: 20 }}>
        <div className="stat-card ok">
          <div className="stat-label">Priority</div>
          <div className="stat-value">{job.priority}</div>
        </div>
        <div className="stat-card ok">
          <div className="stat-label">Retries</div>
          <div className="stat-value">
            {job.current_retry_count}/{job.max_retries}
          </div>
        </div>
        <div className={`stat-card ${job.claimed_by_worker_id ? "ok" : "warn"}`}>
          <div className="stat-label">Claimed by worker</div>
          <div className="stat-value" style={{ fontSize: 14 }}>
            {job.claimed_by_worker_id ? job.claimed_by_worker_id.slice(0, 8) : "—"}
          </div>
        </div>
        {parentIds.length > 0 && (
          <div className="stat-card warn">
            <div className="stat-label">Prerequisites ({parentIds.length})</div>
            <div className="stat-value" style={{ fontSize: 13, display: "flex", flexWrap: "wrap", gap: 4 }}>
              {parentIds.map((pid) => (
                <Link
                  key={pid}
                  href={`/jobs/${pid}`}
                  style={{ color: "var(--signal-blue)", textDecoration: "underline", fontFamily: "var(--font-mono)" }}
                  title={pid}
                >
                  {pid.slice(0, 8)}…
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>

      {(job.status === "failed" || job.status === "dead_letter") && (
        <button className="btn" onClick={retry} style={{ marginBottom: 20 }}>
          Retry this job
        </button>
      )}

      {/* DAG Workflow Topology Visualization */}
      {dag && dag.nodes.length > 1 && (
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, padding: 18, marginBottom: 24 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h3 style={{ fontSize: 14, fontWeight: 600 }}>DAG Dependency Topology</h3>
            <span style={{ fontSize: 12, color: "var(--signal-green)", fontFamily: "var(--font-mono)" }}>
              {dag.nodes.length} Connected Jobs • {dag.edges.length} Directed Edges • Acyclic
            </span>
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
            {dag.nodes.map((node) => {
              const isCurrent = node.id === job.id;
              return (
                <div
                  key={node.id}
                  style={{
                    padding: "10px 14px",
                    background: isCurrent ? "rgba(59, 130, 246, 0.12)" : "var(--bg)",
                    border: `1px solid ${isCurrent ? "var(--signal-blue)" : "var(--border)"}`,
                    borderRadius: 6,
                    minWidth: 160,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                    <Link
                      href={`/jobs/${node.id}`}
                      style={{
                        fontWeight: 600,
                        fontSize: 13,
                        color: isCurrent ? "var(--signal-blue)" : "var(--text)",
                      }}
                    >
                      {node.name}
                    </Link>
                    <span className={`badge ${node.status}`} style={{ fontSize: 10, padding: "1px 5px" }}>
                      {node.status}
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-dim)", fontFamily: "var(--font-mono)" }}>
                    ID: {node.id.slice(0, 8)}…
                  </div>
                  {node.parent_job_ids && node.parent_job_ids.length > 0 && (
                    <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 4 }}>
                      Parents: {node.parent_job_ids.length}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <h2 style={{ fontSize: 14, color: "var(--text-dim)", fontWeight: 600, marginBottom: 10 }}>
        Execution history ({job.executions.length} attempt{job.executions.length === 1 ? "" : "s"})
      </h2>

      {job.executions.length === 0 && (
        <p className="page-subtitle">No execution attempts yet.</p>
      )}

      {[...job.executions].reverse().map((exec) => (
        <div key={exec.id} className="table-wrap" style={{ marginBottom: 14, padding: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>
              Attempt {exec.attempt_number}
            </span>
            <span className={`badge ${exec.status}`}>{exec.status}</span>
          </div>
          <p style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-dim)", margin: "0 0 4px 0" }}>
            worker: {exec.worker_id ? exec.worker_id.slice(0, 8) : "—"} | started:{" "}
            {new Date(exec.started_at).toLocaleTimeString()}
            {exec.finished_at && <> | finished: {new Date(exec.finished_at).toLocaleTimeString()}</>}
          </p>
          {exec.error_message && (
            <p style={{ color: "var(--signal-red)", fontSize: 13, margin: "8px 0" }}>{exec.error_message}</p>
          )}
          {exec.result != null && (
            <pre style={{
              background: "var(--bg)", border: "1px solid var(--border)", padding: 10,
              fontSize: 12, overflowX: "auto", margin: "8px 0",
            }}>
              {JSON.stringify(exec.result, null, 2)}
            </pre>
          )}
          {exec.logs.length > 0 && (
            <div style={{ marginTop: 8 }}>
              {exec.logs.map((log) => (
                <div key={log.id} style={{ fontFamily: "var(--font-mono)", fontSize: 12, padding: "3px 0", color: log.level === "error" ? "var(--signal-red)" : "var(--text-dim)" }}>
                  [{new Date(log.timestamp).toLocaleTimeString()}] {log.level.toUpperCase()} — {log.message}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </>
  );
}
