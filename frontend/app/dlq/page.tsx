"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, DeadLetterJob } from "../../lib/api";
import { useEvents } from "../../lib/useEvents";

export default function DlqPage() {
  const [dlq, setDlq] = useState<DeadLetterJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  async function load() {
    setDlq(await api<DeadLetterJob[]>("/dead-letter-jobs"));
    setLoading(false);
  }

  useEffect(() => { load(); }, []);

  // Real-time updates when jobs enter or exit DLQ
  useEvents((event) => {
    if (event.event === "job.dead_letter" || event.event === "job.retried" || event.event === "job.completed") {
      load();
    }
  });

  async function retryJob(jobId: string) {
    setRetrying(jobId);
    setSuccessMsg(null);
    try {
      await api(`/jobs/${jobId}/retry`, { method: "POST" });
      setSuccessMsg(`Job ${jobId.slice(0, 8)} re-queued.`);
      load(); // refresh to remove retried entry from DLQ view
    } finally {
      setRetrying(null);
    }
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;

  return (
    <>
      <h1 className="page-title">Dead Letter Queue</h1>
      <p className="page-subtitle">
        Jobs that exhausted all retries. Review the failure reason and manually re-queue if appropriate.
      </p>

      <div className="stat-grid" style={{ marginBottom: 24 }}>
        <div className={`stat-card ${dlq.length > 0 ? "danger" : "ok"}`}>
          <div className="stat-label">Dead-lettered jobs</div>
          <div className="stat-value">{dlq.length}</div>
        </div>
      </div>

      {successMsg && (
        <p style={{ color: "var(--signal-green)", fontSize: 13, marginBottom: 14 }}>{successMsg}</p>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Job ID</th>
              <th>Reason</th>
              <th>Retries at failure</th>
              <th>Failed at</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {dlq.length === 0 && (
              <tr>
                <td colSpan={5} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  Nothing dead-lettered. Clean run. 🎉
                </td>
              </tr>
            )}
            {dlq.map((d) => (
              <tr key={d.id}>
                <td>
                  <Link
                    href={`/jobs/${d.job_id}`}
                    style={{ color: "var(--signal-blue)" }}
                    title={d.job_id}
                  >
                    {d.job_id.slice(0, 8)}…
                  </Link>
                </td>
                <td
                  style={{
                    fontFamily: "var(--font-sans)",
                    color: "var(--signal-red)",
                    maxWidth: 360,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={d.reason}
                >
                  {d.reason}
                </td>
                <td>{d.retry_count_at_failure}</td>
                <td>{new Date(d.failed_at).toLocaleString()}</td>
                <td>
                  <button
                    className="btn"
                    onClick={() => retryJob(d.job_id)}
                    disabled={retrying === d.job_id}
                  >
                    {retrying === d.job_id ? "Retrying…" : "Retry"}
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
