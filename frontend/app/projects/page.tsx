"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Project } from "../../lib/api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function loadProjects() {
    try {
      setProjects(await api<Project[]>("/projects"));
    } catch {
      setError("Failed to load projects.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadProjects(); }, []);

  async function createProject(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setError(null);
    try {
      await api("/projects", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), description: description.trim() || undefined }),
      });
      setName("");
      setDescription("");
      loadProjects();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create project.");
    }
  }

  if (loading) return <p className="page-subtitle">Loading…</p>;

  return (
    <>
      <h1 className="page-title">Projects</h1>
      <p className="page-subtitle">Organize queues and jobs into projects within your organization.</p>

      {/* Create project form */}
      <form onSubmit={createProject} className="toolbar" style={{ marginBottom: 20 }}>
        <input
          className="input"
          placeholder="project-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          className="input"
          placeholder="Description (optional)"
          style={{ flex: 1 }}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <button className="btn" type="submit">+ Create project</button>
      </form>

      {error && (
        <p style={{ color: "var(--signal-red)", fontSize: 13, marginBottom: 12 }}>{error}</p>
      )}

      {/* Projects table */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Description</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {projects.length === 0 && (
              <tr>
                <td colSpan={4} style={{ color: "var(--text-faint)", fontFamily: "var(--font-sans)" }}>
                  No projects yet. Create your first one above.
                </td>
              </tr>
            )}
            {projects.map((p) => (
              <tr key={p.id}>
                <td style={{ fontWeight: 600 }}>{p.name}</td>
                <td style={{ fontFamily: "var(--font-sans)", color: "var(--text-dim)" }}>
                  {p.description || "—"}
                </td>
                <td>{new Date(p.created_at).toLocaleDateString()}</td>
                <td>
                  <Link
                    href={`/queues?project=${p.id}`}
                    style={{ color: "var(--signal-blue)", fontFamily: "var(--font-mono)", fontSize: 12 }}
                  >
                    View queues →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
