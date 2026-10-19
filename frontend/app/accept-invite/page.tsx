"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { acceptInvite } from "../../lib/api";

function AcceptInviteForm() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const initialToken = searchParams.get("token") || "";
  const initialEmail = searchParams.get("email") || "";

  const [token, setToken] = useState(initialToken);
  const [email, setEmail] = useState(initialEmail);
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!token.trim()) {
      setError("Invite token is required.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setLoading(true);
    try {
      await acceptInvite(token.trim(), email.trim(), password, fullName.trim() || undefined);
      router.push("/");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to accept invite.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={handleSubmit} style={{ width: 380 }}>
        <div className="brand" style={{ border: "none", marginBottom: 20 }}>
          <span className="brand-mark" />
          <span className="brand-name">PULSE</span>
        </div>

        <h2 style={{ fontSize: 16, fontWeight: 700, margin: "0 0 6px 0" }}>Accept Invitation</h2>
        <p style={{ fontSize: 13, color: "var(--text-dim)", margin: "0 0 20px 0" }}>
          Set up your account to join your company workspace.
        </p>

        <div className="field">
          <label>Invite Token</label>
          <input
            className="input"
            type="text"
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Paste token or use link"
            style={{ fontFamily: "var(--font-mono)" }}
          />
        </div>

        <div className="field">
          <label>Email Address</label>
          <input
            className="input"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com"
          />
        </div>

        <div className="field">
          <label>Full Name</label>
          <input
            className="input"
            type="text"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            placeholder="Alex Rivera"
          />
        </div>

        <div className="field">
          <label>Password (min 8 characters)</label>
          <input
            className="input"
            type="password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {error && (
          <p style={{ color: "var(--signal-red)", fontSize: 12, marginBottom: 12 }}>{error}</p>
        )}

        <button
          className="btn"
          type="submit"
          style={{ width: "100%", marginBottom: 14 }}
          disabled={loading}
        >
          {loading ? "Joining team…" : "Join Organization"}
        </button>

        <p style={{ fontSize: 12, color: "var(--text-faint)", textAlign: "center", margin: 0 }}>
          Already have an account?{" "}
          <Link href="/login" style={{ color: "var(--signal-blue)" }}>
            Sign in
          </Link>
        </p>
      </form>
    </div>
  );
}

export default function AcceptInvitePage() {
  return (
    <Suspense fallback={<div className="login-wrap"><p className="page-subtitle">Loading invite...</p></div>}>
      <AcceptInviteForm />
    </Suspense>
  );
}
