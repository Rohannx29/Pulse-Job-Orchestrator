"use client";

import { useEffect, useState } from "react";
import { api, getMe, Org, OrgMember, User } from "../../lib/api";

export default function TeamPage() {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [org, setOrg] = useState<Org | null>(null);
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [loading, setLoading] = useState(true);

  // Invite state
  const [inviteEmail, setInviteEmail] = useState("");
  const [generatedInvite, setGeneratedInvite] = useState<{ token: string; link: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviting, setInviting] = useState(false);

  async function loadData() {
    try {
      const [me, myOrg, orgMembers] = await Promise.all([
        getMe(),
        api<Org>("/orgs/me"),
        api<OrgMember[]>("/orgs/me/members"),
      ]);
      setCurrentUser(me);
      setOrg(myOrg);
      setMembers(orgMembers);
    } catch (err: unknown) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, []);

  const isAdmin = currentUser?.role === "system_admin" || currentUser?.role === "org_admin";

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setInviteError(null);
    setGeneratedInvite(null);
    setCopied(false);

    if (!inviteEmail.trim()) return;

    setInviting(true);
    try {
      const res = await api<{ invite_token: string; email: string; expires_at: string }>("/orgs/me/invite", {
        method: "POST",
        body: JSON.stringify({ email: inviteEmail.trim() }),
      });

      const origin = typeof window !== "undefined" ? window.location.origin : "";
      const inviteUrl = `${origin}/accept-invite?token=${encodeURIComponent(res.invite_token)}&email=${encodeURIComponent(res.email)}`;

      setGeneratedInvite({
        token: res.invite_token,
        link: inviteUrl,
      });
      setInviteEmail("");
    } catch (err: unknown) {
      setInviteError(err instanceof Error ? err.message : "Failed to generate invite.");
    } finally {
      setInviting(false);
    }
  }

  async function handleRemoveMember(memberId: string) {
    if (!confirm("Are you sure you want to remove this member from the organization?")) return;
    try {
      await api(`/orgs/me/members/${memberId}`, { method: "DELETE" });
      loadData();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Failed to remove member.");
    }
  }

  function copyToClipboard(text: string) {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  }

  if (loading) return <p className="page-subtitle">Loading team…</p>;

  return (
    <>
      <h1 className="page-title">{org?.name ? `${org.name} — Team` : "Team"}</h1>
      <p className="page-subtitle">
        Manage members and role permissions for this organization.
      </p>

      {/* Overview Cards */}
      <div className="stat-grid" style={{ marginBottom: 24 }}>
        <div className="stat-card ok">
          <div className="stat-label">Total Members</div>
          <div className="stat-value">{members.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Your Role</div>
          <div className="stat-value" style={{ fontSize: 18, textTransform: "uppercase" }}>
            {currentUser?.role === "system_admin"
              ? "System Admin"
              : currentUser?.role === "org_admin"
              ? "Org Admin"
              : "Member"}
          </div>
        </div>
      </div>

      {/* Invite Member Section (Available for Admins) */}
      {isAdmin && (
        <div className="table-wrap" style={{ padding: 18, marginBottom: 24 }}>
          <p style={{ fontSize: 13, color: "var(--text)", fontWeight: 600, margin: "0 0 4px 0" }}>
            Invite new team member
          </p>
          <p style={{ fontSize: 12, color: "var(--text-dim)", margin: "0 0 14px 0" }}>
            Generate a secure, single-use invite link. Invited users join as normal team members.
          </p>

          <form onSubmit={handleInvite} style={{ display: "flex", gap: 10, maxWidth: 500, marginBottom: 12 }}>
            <input
              className="input"
              type="email"
              placeholder="colleague@company.com"
              required
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              style={{ flex: 1 }}
            />
            <button className="btn" type="submit" disabled={inviting}>
              {inviting ? "Generating…" : "+ Send Invite"}
            </button>
          </form>

          {inviteError && (
            <p style={{ color: "var(--signal-red)", fontSize: 12, margin: "0 0 10px 0" }}>{inviteError}</p>
          )}

          {generatedInvite && (
            <div
              style={{
                background: "var(--bg)",
                border: "1px solid var(--border)",
                padding: 12,
                borderRadius: 4,
                marginTop: 10,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                <span style={{ fontSize: 12, color: "var(--signal-green)", fontWeight: 600 }}>
                  ✓ Invite link generated (valid for 72 hours)
                </span>
                <button
                  type="button"
                  className="btn"
                  onClick={() => copyToClipboard(generatedInvite.link)}
                  style={{ fontSize: 11, padding: "3px 8px" }}
                >
                  {copied ? "Copied! ✓" : "Copy Link"}
                </button>
              </div>
              <input
                className="input"
                readOnly
                value={generatedInvite.link}
                style={{ width: "100%", fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--signal-blue)" }}
              />
            </div>
          )}
        </div>
      )}

      {/* Members Table */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Member</th>
              <th>Email</th>
              <th>Role</th>
              <th>Joined</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const isOwner = org?.owner_id === m.user_id;
              const isMe = currentUser?.id === m.user_id;

              return (
                <tr key={m.id}>
                  <td style={{ fontWeight: 600 }}>
                    {m.full_name || "—"} {isMe && <span style={{ color: "var(--text-faint)", fontSize: 11 }}>(You)</span>}
                  </td>
                  <td style={{ fontFamily: "var(--font-mono)" }}>{m.email}</td>
                  <td>
                    <span className={`badge ${m.role === "admin" ? "completed" : "queued"}`}>
                      {isOwner ? "Owner (Admin)" : m.role}
                    </span>
                  </td>
                  <td>{new Date(m.created_at).toLocaleDateString()}</td>
                  <td>
                    {isAdmin && !isOwner && !isMe && (
                      <button
                        className="btn"
                        onClick={() => handleRemoveMember(m.id)}
                        style={{ color: "var(--signal-red)", fontSize: 11 }}
                      >
                        Remove
                      </button>
                    )}
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
