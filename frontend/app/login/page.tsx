"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { login } from "../../lib/api";

function LoginForm() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  const searchParams = useSearchParams();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await login(email, password);
      // Redirect to the originally requested page, or dashboard
      const next = searchParams.get("next") || "/";
      router.push(next);
    } catch {
      setError("Invalid email or password.");
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="brand" style={{ border: "none", marginBottom: 20 }}>
          <span className="brand-mark" />
          <span className="brand-name">PULSE</span>
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
          <label>Password</label>
          <input
            className="input"
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error && (
          <p style={{ color: "var(--signal-red)", fontSize: 12, marginBottom: 12 }}>{error}</p>
        )}
        <button className="btn" type="submit" style={{ width: "100%", marginBottom: 14 }}>
          Sign in
        </button>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--text-faint)" }}>
          <Link href="/signup" style={{ color: "var(--signal-blue)" }}>
            Register Organization
          </Link>
          <Link href="/accept-invite" style={{ color: "var(--signal-green)" }}>
            Have an invite? Join
          </Link>
        </div>
      </form>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="login-wrap"><p className="page-subtitle">Loading...</p></div>}>
      <LoginForm />
    </Suspense>
  );
}
