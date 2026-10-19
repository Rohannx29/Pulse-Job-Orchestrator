"use client";

import { Fragment, useEffect, useState } from "react";
import { AuditLog, getAuditLogs, getMe, User } from "../../lib/api";

const ACTIONS = [
  "all",
  "queue.pause",
  "queue.resume",
  "queue.update",
  "job.retry",
  "member.invite",
  "member.remove",
  "user.role_change",
  "schedule.toggle",
];

const RESOURCE_TYPES = ["all", "queue", "job", "organization", "user", "schedule"];

export default function AuditLogsPage() {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [actionFilter, setActionFilter] = useState("all");
  const [resourceFilter, setResourceFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);

  async function loadLogs() {
    setLoading(true);
    try {
      const me = await getMe();
      setCurrentUser(me);

      const items = await getAuditLogs({
        action: actionFilter === "all" ? undefined : actionFilter,
        resource_type: resourceFilter === "all" ? undefined : resourceFilter,
        limit: 100,
      });
      setLogs(items);
    } catch (err: unknown) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadLogs();
  }, [actionFilter, resourceFilter]); // eslint-disable-line react-hooks/exhaustive-deps

  function getBadgeClass(action: string) {
    if (action.includes("pause") || action.includes("remove") || action.includes("delete")) {
      return "dead_letter";
    }
    if (action.includes("resume") || action.includes("invite")) {
      return "completed";
    }
    if (action.includes("retry") || action.includes("update") || action.includes("role")) {
      return "running";
    }
    return "queued";
  }

  function toggleExpand(id: string) {
    setExpanded(expanded === id ? null : id);
  }

  return (
    <>
      <h1 className="page-title">Audit Log & Activity Trail</h1>
      <p className="page-subtitle">
        Immutable, compliance-ready record of all sensitive operator actions and configuration changes.
      </p>

      {/* Filters Toolbar */}
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 10 }}>
        <div>
          <label style={{ fontSize: 11, color: "var(--text-faint)", display: "block", marginBottom: 3 }}>
            ACTION
          </label>
          <select
            className="select"
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
          >
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label style={{ fontSize: 11, color: "var(--text-faint)", display: "block", marginBottom: 3 }}>
            RESOURCE TYPE
          </label>
          <select
            className="select"
            value={resourceFilter}
            onChange={(e) => setResourceFilter(e.target.value)}
          >
            {RESOURCE_TYPES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>

        <button className="btn" onClick={loadLogs} style={{ marginTop: "auto" }}>
          Refresh
        </button>
      </div>

      {loading && <p className="page-subtitle">Loading audit logs…</p>}

      {!loading && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Action</th>
                <th>Actor</th>
                <th>Target Resource</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {logs.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                    No audit records match the selected filters.
                  </td>
                </tr>
              )}
              {logs.map((log) => {
                const isExpanded = expanded === log.id;
                const dateObj = new Date(log.created_at);

                return (
                  <Fragment key={log.id}>
                    <tr>
                      <td style={{ fontSize: 12, color: "var(--text-dim)" }}>
                        {dateObj.toLocaleDateString()} {dateObj.toLocaleTimeString()}
                      </td>
                      <td>
                        <span className={`badge ${getBadgeClass(log.action)}`}>
                          {log.action}
                        </span>
                      </td>
                      <td style={{ fontFamily: "var(--font-sans)" }}>
                        <div style={{ fontWeight: 600, fontSize: 13 }}>
                          {log.user_name || log.user_email || "System"}
                        </div>
                        {log.user_name && log.user_email && (
                          <div style={{ fontSize: 11, color: "var(--text-faint)", fontFamily: "var(--font-mono)" }}>
                            {log.user_email}
                          </div>
                        )}
                      </td>
                      <td>
                        <span style={{ color: "var(--text-faint)", fontSize: 12 }}>
                          {log.resource_type}:
                        </span>{" "}
                        <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>
                          {log.resource_id ? log.resource_id.slice(0, 10) : "—"}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn"
                          onClick={() => toggleExpand(log.id)}
                          style={{ fontSize: 11, padding: "2px 8px" }}
                        >
                          {isExpanded ? "Hide ▴" : "Inspect ▾"}
                        </button>
                      </td>
                    </tr>

                    {isExpanded && (
                      <tr>
                        <td colSpan={5} style={{ background: "var(--bg)", padding: 14 }}>
                          <div style={{ marginBottom: 6, fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase" }}>
                            Metadata & Context
                          </div>
                          <pre
                            style={{
                              background: "var(--surface)",
                              border: "1px solid var(--border)",
                              padding: 10,
                              borderRadius: 4,
                              fontSize: 12,
                              overflowX: "auto",
                              margin: 0,
                              fontFamily: "var(--font-mono)",
                              color: "var(--text)",
                            }}
                          >
                            {JSON.stringify(
                              {
                                id: log.id,
                                org_id: log.org_id,
                                user_id: log.user_id,
                                action: log.action,
                                resource_type: log.resource_type,
                                resource_id: log.resource_id,
                                details: log.details,
                                created_at: log.created_at,
                              },
                              null,
                              2
                            )}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
