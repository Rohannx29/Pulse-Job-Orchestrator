"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AdminOrg, AdminStats, AdminUser, api, getMe, getSystemHealth, SystemHealth, User, API_URL } from "../../lib/api";

export default function AdminPage() {
  const router = useRouter();
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [orgs, setOrgs] = useState<AdminOrg[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [roleUpdating, setRoleUpdating] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function loadData() {
    try {
      const me = await getMe();
      setCurrentUser(me);
      if (me.role !== "system_admin") {
        setError("Access restricted. System administrator privileges are required.");
        setLoading(false);
        return;
      }

      const [adminStats, adminOrgs, adminUsers, sysHealth] = await Promise.all([
        api<AdminStats>("/admin/stats"),
        api<AdminOrg[]>("/admin/organizations"),
        api<AdminUser[]>("/admin/users"),
        getSystemHealth().catch(() => null),
      ]);

      setStats(adminStats);
      setOrgs(adminOrgs);
      setUsers(adminUsers);
      setHealth(sysHealth);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load system admin data.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, []);

  const [pruning, setPruning] = useState(false);
  const [pruneResult, setPruneResult] = useState<string | null>(null);

  async function handleRoleChange(userId: string, newRole: string) {
    setRoleUpdating(userId);
    try {
      await api(`/admin/users/${userId}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role: newRole }),
      });
      // Refresh user list
      const updated = await api<AdminUser[]>("/admin/users");
      setUsers(updated);
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Failed to update role");
    } finally {
      setRoleUpdating(null);
    }
  }

  async function handlePruneData() {
    if (!confirm("Run automated data retention prune? This permanently deletes heartbeats older than 7d, execution logs older than 30d, and completed jobs older than 60d.")) {
      return;
    }
    setPruning(true);
    setPruneResult(null);
    try {
      const res = await api<{
        total_pruned_rows: number;
        pruned_heartbeats: number;
        pruned_job_logs: number;
        pruned_job_executions: number;
        pruned_completed_jobs: number;
        duration_ms: number;
      }>("/admin/prune", { method: "POST" });
      setPruneResult(`Pruned ${res.total_pruned_rows} rows in ${res.duration_ms}ms (heartbeats: ${res.pruned_heartbeats}, logs: ${res.pruned_job_logs}, executions: ${res.pruned_job_executions}, jobs: ${res.pruned_completed_jobs}).`);
      loadData();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Pruning failed");
    } finally {
      setPruning(false);
    }
  }

  if (loading) return <p className="page-subtitle">Loading admin console…</p>;

  if (error) {
    return (
      <div style={{ padding: "40px 0" }}>
        <h1 className="page-title" style={{ color: "var(--signal-red)" }}>Access Denied</h1>
        <p className="page-subtitle">{error}</p>
        <button className="btn" onClick={() => router.push("/")}>Return to Dashboard</button>
      </div>
    );
  }

  return (
    <>
      <h1 className="page-title">Platform Administration</h1>
      <p className="page-subtitle">
        System-level control over all tenants, organizations, user accounts, and platform infrastructure.
      </p>

      {/* Cluster Health & Metrics */}
      {health && (
        <div style={{
          background: "var(--card-bg)",
          border: `1px solid ${health.status === "healthy" ? "rgba(16, 185, 129, 0.3)" : "rgba(239, 68, 68, 0.3)"}`,
          borderRadius: 8,
          padding: "16px 20px",
          marginBottom: 24,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 16
        }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <span style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: "50%",
                backgroundColor: health.status === "healthy" ? "#10b981" : "#ef4444"
              }} />
              <strong style={{ fontSize: 14, color: "var(--text)" }}>
                Cluster Health: {health.status === "healthy" ? "All Systems Operational" : "Degraded"}
              </strong>
              <span style={{ fontSize: 12, color: "var(--text-dim)" }}>v{health.version}</span>
            </div>
            <div style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
              <span>
                PostgreSQL:{" "}
                <strong style={{ color: health.components.database?.status === "healthy" ? "#10b981" : "#ef4444" }}>
                  {health.components.database?.status === "healthy" ? `UP (${health.components.database?.latency_ms}ms)` : "DOWN"}
                </strong>
              </span>
              <span>
                Redis Broker:{" "}
                <strong style={{ color: health.components.redis?.status === "healthy" ? "#10b981" : "#ef4444" }}>
                  {health.components.redis?.status === "healthy" ? `UP (${health.components.redis?.latency_ms}ms)` : "DOWN"}
                </strong>
              </span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <a
              href={`${API_URL}/metrics`}
              target="_blank"
              rel="noreferrer"
              className="btn btn-secondary"
              style={{ fontSize: 12, padding: "6px 12px", textDecoration: "none" }}
            >
              Prometheus Metrics (0.0.4) ↗
            </a>
            <button
              onClick={loadData}
              className="btn btn-secondary"
              style={{ fontSize: 12, padding: "6px 12px" }}
              title="Refresh health status"
            >
              ↻ Refresh
            </button>
            <button
              onClick={handlePruneData}
              disabled={pruning}
              className="btn btn-secondary"
              style={{ fontSize: 12, padding: "6px 12px", color: "var(--signal-amber, #f59e0b)", borderColor: "rgba(245, 158, 11, 0.4)" }}
              title="Prune stale logs, heartbeats, and completed jobs according to retention policy"
            >
              {pruning ? "Pruning…" : "🗑 Prune Stale Data"}
            </button>
          </div>
        </div>
      )}

      {pruneResult && (
        <div style={{
          background: "rgba(16, 185, 129, 0.1)",
          border: "1px solid rgba(16, 185, 129, 0.3)",
          color: "var(--text)",
          padding: "10px 14px",
          borderRadius: 6,
          fontSize: 13,
          marginBottom: 20
        }}>
          ✓ {pruneResult}
        </div>
      )}

      {/* Platform Stats Grid */}
      {stats && (
        <div className="stat-grid" style={{ marginBottom: 28 }}>
          <div className="stat-card ok">
            <div className="stat-label">Organizations</div>
            <div className="stat-value">{stats.total_organizations}</div>
          </div>
          <div className="stat-card ok">
            <div className="stat-label">Total Users</div>
            <div className="stat-value">{stats.total_users}</div>
          </div>
          <div className="stat-card ok">
            <div className="stat-label">Total Projects</div>
            <div className="stat-value">{stats.total_projects}</div>
          </div>
          <div className="stat-card ok">
            <div className="stat-label">Queues</div>
            <div className="stat-value">{stats.total_queues}</div>
          </div>
          <div className="stat-card ok">
            <div className="stat-label">Total Jobs</div>
            <div className="stat-value">{stats.total_jobs}</div>
          </div>
        </div>
      )}

      {/* Organizations Section */}
      <h2 style={{ fontSize: 15, fontWeight: 600, color: "var(--text)", margin: "0 0 10px 0" }}>
        All Organizations ({orgs.length})
      </h2>
      <div className="table-wrap" style={{ marginBottom: 32 }}>
        <table>
          <thead>
            <tr>
              <th>Organization</th>
              <th>Owner ID</th>
              <th>Members</th>
              <th>Created At</th>
            </tr>
          </thead>
          <tbody>
            {orgs.map((o) => (
              <tr key={o.id}>
                <td style={{ fontWeight: 600 }}>{o.name}</td>
                <td style={{ fontFamily: "var(--font-mono)", color: "var(--text-dim)" }}>
                  {o.owner_id.slice(0, 8)}…
                </td>
                <td>{o.member_count}</td>
                <td>{new Date(o.created_at).toLocaleDateString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Users Section */}
      <h2 style={{ fontSize: 15, fontWeight: 600, color: "var(--text)", margin: "0 0 10px 0" }}>
        All Users ({users.length})
      </h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th>System Role</th>
              <th>Registered</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const isMe = currentUser?.id === u.id;

              return (
                <tr key={u.id}>
                  <td style={{ fontWeight: 600 }}>
                    {u.full_name || "—"} {isMe && <span style={{ color: "var(--text-faint)", fontSize: 11 }}>(You)</span>}
                  </td>
                  <td style={{ fontFamily: "var(--font-mono)" }}>{u.email}</td>
                  <td>
                    <span
                      className={`badge ${
                        u.role === "system_admin"
                          ? "completed"
                          : u.role === "org_admin"
                          ? "running"
                          : "queued"
                      }`}
                    >
                      {u.role}
                    </span>
                  </td>
                  <td>{new Date(u.created_at).toLocaleDateString()}</td>
                  <td>
                    <select
                      className="select"
                      disabled={roleUpdating === u.id || (isMe && u.role === "system_admin")}
                      value={u.role}
                      onChange={(e) => handleRoleChange(u.id, e.target.value)}
                      style={{ fontSize: 12, padding: "3px 6px" }}
                    >
                      <option value="system_admin">system_admin</option>
                      <option value="org_admin">org_admin</option>
                      <option value="member">member</option>
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
