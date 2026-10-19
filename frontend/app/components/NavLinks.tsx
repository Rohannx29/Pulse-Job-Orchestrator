"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { getMe, logout, User } from "../../lib/api";
import { useEvents } from "../../lib/useEvents";

const BASE_LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/projects", label: "Projects" },
  { href: "/queues", label: "Queues" },
  { href: "/jobs", label: "Jobs" },
  { href: "/schedules", label: "Schedules" },
  { href: "/workers", label: "Workers" },
  { href: "/dlq", label: "DLQ" },
  { href: "/webhooks", label: "Webhooks" },
  { href: "/team", label: "Team" },
  { href: "/audit-logs", label: "Audit Logs" },
];


export default function NavLinks() {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const { status: sseStatus } = useEvents();

  useEffect(() => {
    getMe()
      .then((u) => setUser(u))
      .catch(() => {});
  }, [pathname]);

  async function handleLogout() {
    await logout();
    router.push("/login");
  }

  const links = [...BASE_LINKS];
  if (user?.role === "system_admin") {
    links.push({ href: "/admin", label: "Admin Console" });
  }

  return (
    <>
      {links.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={`nav-link ${pathname === link.href ? "active" : ""}`}
        >
          {link.label}
        </Link>
      ))}

      <div style={{ marginTop: "auto", paddingTop: 16, borderTop: "1px solid var(--border)" }}>
        <div style={{ padding: "0 10px 8px 10px", display: "flex", alignItems: "center", gap: 6 }}>
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              backgroundColor:
                sseStatus === "connected"
                  ? "var(--signal-green)"
                  : sseStatus === "connecting"
                  ? "var(--signal-amber)"
                  : "var(--signal-red)",
              display: "inline-block",
              boxShadow: sseStatus === "connected" ? "0 0 6px var(--signal-green)" : "none",
            }}
          />
          <span style={{ color: "var(--text-dim)", fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.5px" }}>
            {sseStatus === "connected" ? "SSE LIVE" : sseStatus === "connecting" ? "CONNECTING…" : "DISCONNECTED"}
          </span>
        </div>
        {user && (
          <div style={{ padding: "8px 10px 12px 10px" }}>
            <div
              style={{
                fontSize: 12,
                fontWeight: 600,
                color: "var(--text)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={user.email}
            >
              {user.full_name || user.email}
            </div>
            <div style={{ marginTop: 4 }}>
              <span
                className={`badge ${
                  user.role === "system_admin"
                    ? "completed"
                    : user.role === "org_admin"
                    ? "running"
                    : "queued"
                }`}
                style={{ fontSize: 10, padding: "1px 6px" }}
              >
                {user.role === "system_admin"
                  ? "SYSTEM ADMIN"
                  : user.role === "org_admin"
                  ? "ORG ADMIN"
                  : "MEMBER"}
              </span>
            </div>
          </div>
        )}

        <button
          onClick={handleLogout}
          style={{
            background: "none",
            border: "none",
            color: "var(--text-faint)",
            cursor: "pointer",
            fontFamily: "var(--font-sans)",
            fontSize: 14,
            fontWeight: 500,
            padding: "8px 10px",
            textAlign: "left",
            width: "100%",
            borderRadius: 6,
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.color = "var(--signal-red)";
            (e.currentTarget as HTMLButtonElement).style.background = "var(--surface)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.color = "var(--text-faint)";
            (e.currentTarget as HTMLButtonElement).style.background = "none";
          }}
        >
          Sign out
        </button>
      </div>
    </>
  );
}
