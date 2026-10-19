"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { register, login } from "../../lib/api";

export default function SignupPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const router = useRouter();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    try {
      await register(email, password, fullName, orgName);
      // Auto-login after successful registration
      await login(email, password);
      router.push("/");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Registration failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="brand" style={{ border: "none", marginBottom: 20 }}>
          <span className="brand-mark" />
          <span className="brand-name">PULSE</span>
        </div>

        <p style={{ fontSize: 13, color: "var(--text-dim)", margin: "0 0 20px 0" }}>
          Create your account and organization.
        </p>

        <div className="field">
          <label>Full Name</label>
          <input
            className="input"
            type="text"
            placeholder="Jane Smith"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
          />
        </div>
        <div className="field">
          <label>Organization Name</label>
          <input
            className="input"
            type="text"
            placeholder="Acme Corp"
            required
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
          />
        </div>
        <div className="field">
          <label>Email</label>
          <input
            className="input"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label>Password (min 8 chars)</label>
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

        <button className="btn" type="submit" style={{ width: "100%", marginBottom: 14 }} disabled={loading}>
          {loading ? "Creating account…" : "Create account"}
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
