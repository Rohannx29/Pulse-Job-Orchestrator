import type { Metadata } from "next";
import "./globals.css";
import NavLinks from "./components/NavLinks";

export const metadata: Metadata = {
  title: "Pulse — Job Scheduler",
  description: "Distributed job scheduling & orchestration console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <aside className="sidebar">
            <div className="brand">
              <span className="brand-mark" />
              <span className="brand-name">PULSE</span>
            </div>
            <NavLinks />
            <div style={{ marginTop: "auto", paddingTop: 20 }}>
              <svg className="pulse-trace" viewBox="0 0 84 28" preserveAspectRatio="none">
                <path d="M0 14 H24 L30 4 L36 24 L42 14 H84" />
              </svg>
            </div>
          </aside>
          <main className="main">{children}</main>
        </div>
      </body>
    </html>
  );
}
