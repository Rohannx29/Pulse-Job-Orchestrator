"use client";

import { useEffect, useState } from "react";
import {
  createWebhook,
  deleteWebhook,
  getWebhookDeliveries,
  getWebhooks,
  testWebhook,
  updateWebhook,
  WebhookDelivery,
  WebhookSubscription,
  WebhookTestResponse,
} from "../../lib/api";
import { useEvents } from "../../lib/useEvents";

export default function WebhooksPage() {
  const [webhooks, setWebhooks] = useState<WebhookSubscription[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Form states
  const [url, setUrl] = useState("");
  const [eventsStr, setEventsStr] = useState("*");
  const [description, setDescription] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Selected subscription for deliveries modal/drawer
  const [selectedSub, setSelectedSub] = useState<WebhookSubscription | null>(null);
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [loadingDeliveries, setLoadingDeliveries] = useState(false);

  // Testing status
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<WebhookTestResponse | null>(null);

  // Visible secrets map
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, boolean>>({});

  async function loadWebhooks() {
    try {
      setError(null);
      const data = await getWebhooks();
      setWebhooks(data);
    } catch (err: any) {
      setError(err.message || "Failed to load webhooks");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadWebhooks();
  }, []);

  // Listen to live events
  useEvents((event) => {
    if (event.event.startsWith("webhook.")) {
      loadWebhooks();
      if (selectedSub) {
        loadDeliveries(selectedSub.id);
      }
    }
  });

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;

    setIsSubmitting(true);
    setError(null);
    setSuccessMsg(null);

    const parsedEvents = eventsStr
      .split(",")
      .map((ev) => ev.trim())
      .filter(Boolean);

    try {
      const created = await createWebhook({
        url: url.trim(),
        events: parsedEvents.length > 0 ? parsedEvents : ["*"],
        description: description.trim() || undefined,
      });
      setSuccessMsg(`Webhook registered successfully with secret ${created.secret.slice(0, 10)}…`);
      setUrl("");
      setDescription("");
      setEventsStr("*");
      await loadWebhooks();
    } catch (err: any) {
      setError(err.message || "Failed to create webhook");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleToggleActive(sub: WebhookSubscription) {
    try {
      await updateWebhook(sub.id, { is_active: !sub.is_active });
      await loadWebhooks();
    } catch (err: any) {
      setError(err.message || "Failed to update webhook");
    }
  }

  async function handleDelete(id: string) {
    if (!confirm("Are you sure you want to delete this webhook subscription? All delivery history will be removed.")) {
      return;
    }
    try {
      await deleteWebhook(id);
      if (selectedSub?.id === id) {
        setSelectedSub(null);
        setDeliveries([]);
      }
      await loadWebhooks();
    } catch (err: any) {
      setError(err.message || "Failed to delete webhook");
    }
  }

  async function handleTest(id: string) {
    setTestingId(id);
    setTestResult(null);
    setError(null);
    try {
      const res = await testWebhook(id);
      setTestResult(res);
      if (selectedSub?.id === id) {
        loadDeliveries(id);
      }
    } catch (err: any) {
      setError(err.message || "Failed to trigger test webhook");
    } finally {
      setTestingId(null);
    }
  }

  async function loadDeliveries(id: string) {
    setLoadingDeliveries(true);
    try {
      const data = await getWebhookDeliveries(id);
      setDeliveries(data);
    } catch (err: any) {
      setError(err.message || "Failed to load delivery logs");
    } finally {
      setLoadingDeliveries(false);
    }
  }

  function handleSelectSub(sub: WebhookSubscription) {
    setSelectedSub(sub);
    setTestResult(null);
    loadDeliveries(sub.id);
  }

  function toggleSecretReveal(id: string) {
    setRevealedSecrets((prev) => ({ ...prev, [id]: !prev[id] }));
  }

  if (loading) return <p className="page-subtitle">Loading webhooks…</p>;

  const activeCount = webhooks.filter((w) => w.is_active).length;

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h1 className="page-title">Outbound Webhooks</h1>
          <p className="page-subtitle">
            Reliable event notification engine with HMAC-SHA256 request signatures and automated SSRF protection.
          </p>
        </div>
      </div>

      <div className="stat-grid" style={{ marginBottom: 24 }}>
        <div className="stat-card ok">
          <div className="stat-label">Active Subscriptions</div>
          <div className="stat-value">{activeCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Registered</div>
          <div className="stat-value">{webhooks.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Signature Standard</div>
          <div className="stat-value" style={{ fontSize: 18 }}>HMAC-SHA256 (v1)</div>
        </div>
      </div>

      {error && (
        <div style={{ background: "rgba(239, 68, 68, 0.1)", border: "1px solid var(--signal-red)", padding: 12, borderRadius: 8, color: "var(--signal-red)", marginBottom: 16 }}>
          {error}
        </div>
      )}

      {successMsg && (
        <div style={{ background: "rgba(34, 197, 94, 0.1)", border: "1px solid var(--signal-green)", padding: 12, borderRadius: 8, color: "var(--signal-green)", marginBottom: 16 }}>
          {successMsg}
        </div>
      )}

      {testResult && (
        <div style={{
          background: testResult.is_success ? "rgba(34, 197, 94, 0.1)" : "rgba(239, 68, 68, 0.1)",
          border: `1px solid ${testResult.is_success ? "var(--signal-green)" : "var(--signal-red)"}`,
          padding: 12,
          borderRadius: 8,
          marginBottom: 16,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}>
          <div>
            <strong>Test Ping Outcome: {testResult.is_success ? "SUCCESS" : "FAILED"}</strong>
            <span style={{ marginLeft: 12, fontFamily: "var(--font-mono)", fontSize: 13 }}>
              Status: {testResult.status_code || "Connection Error"} • Latency: {testResult.duration_ms ?? "--"}ms
            </span>
          </div>
          <button className="btn" style={{ padding: "4px 8px", fontSize: 12 }} onClick={() => setTestResult(null)}>
            Dismiss
          </button>
        </div>
      )}

      {/* Registration Form */}
      <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, padding: 20, marginBottom: 28 }}>
        <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>Register New Webhook Endpoint</h3>
        <form onSubmit={handleCreate} style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr auto", gap: 12, alignItems: "end" }}>
          <div>
            <label style={{ display: "block", fontSize: 12, color: "var(--text-dim)", marginBottom: 4 }}>
              Target Endpoint URL (HTTPS recommended)
            </label>
            <input
              type="url"
              className="input"
              placeholder="https://api.example.com/webhooks/pulse"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, color: "var(--text-dim)", marginBottom: 4 }}>
              Events Filter (Comma-separated or *)
            </label>
            <input
              type="text"
              className="input"
              placeholder="*, job.completed, job.failed"
              value={eventsStr}
              onChange={(e) => setEventsStr(e.target.value)}
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, color: "var(--text-dim)", marginBottom: 4 }}>
              Description / Label (Optional)
            </label>
            <input
              type="text"
              className="input"
              placeholder="Production event sink"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <button type="submit" className="btn btn-primary" disabled={isSubmitting} style={{ whiteSpace: "nowrap" }}>
              {isSubmitting ? "Registering…" : "Register Webhook"}
            </button>
          </div>
        </form>
      </div>

      {/* Webhook Subscriptions Table */}
      <div className="table-wrap" style={{ marginBottom: 32 }}>
        <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>Configured Subscriptions</span>
          <span style={{ fontSize: 12, color: "var(--text-dim)" }}>Signed via X-Pulse-Signature</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Target URL</th>
              <th>Signing Secret</th>
              <th>Events Filter</th>
              <th>Status</th>
              <th>Created</th>
              <th style={{ textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {webhooks.length === 0 && (
              <tr>
                <td colSpan={6} style={{ color: "var(--text-faint)", textAlign: "center", padding: 24 }}>
                  No webhook endpoints configured. Register an endpoint above to begin receiving push events.
                </td>
              </tr>
            )}
            {webhooks.map((sub) => {
              const isRevealed = revealedSecrets[sub.id];
              const isSelected = selectedSub?.id === sub.id;
              return (
                <tr key={sub.id} style={{ background: isSelected ? "rgba(59, 130, 246, 0.08)" : undefined }}>
                  <td style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <div style={{ fontFamily: "var(--font-mono)", fontWeight: 500 }} title={sub.url}>
                      {sub.url}
                    </div>
                    {sub.description && (
                      <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{sub.description}</div>
                    )}
                  </td>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <code style={{ fontSize: 11 }}>
                        {isRevealed ? sub.secret : `${sub.secret.slice(0, 10)}••••••••••••••••`}
                      </code>
                      <button
                        type="button"
                        onClick={() => toggleSecretReveal(sub.id)}
                        style={{ background: "none", border: "none", cursor: "pointer", color: "var(--signal-blue)", fontSize: 11 }}
                      >
                        {isRevealed ? "Hide" : "Show"}
                      </button>
                    </div>
                  </td>
                  <td>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                      {sub.events.map((ev) => (
                        <span key={ev} className="badge queued" style={{ fontSize: 11, padding: "1px 6px" }}>
                          {ev}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>
                    <button
                      onClick={() => handleToggleActive(sub)}
                      className={`badge ${sub.is_active ? "completed" : "failed"}`}
                      style={{ cursor: "pointer", border: "none" }}
                      title="Click to toggle active state"
                    >
                      {sub.is_active ? "ACTIVE" : "PAUSED"}
                    </button>
                  </td>
                  <td style={{ fontSize: 12, color: "var(--text-dim)", whiteSpace: "nowrap" }}>
                    {new Date(sub.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button
                      onClick={() => handleTest(sub.id)}
                      disabled={testingId === sub.id}
                      className="btn"
                      style={{ padding: "4px 8px", fontSize: 12, marginRight: 6 }}
                    >
                      {testingId === sub.id ? "Pinging…" : "Test Ping"}
                    </button>
                    <button
                      onClick={() => handleSelectSub(sub)}
                      className="btn"
                      style={{
                        padding: "4px 8px",
                        fontSize: 12,
                        marginRight: 6,
                        borderColor: isSelected ? "var(--signal-blue)" : undefined,
                        color: isSelected ? "var(--signal-blue)" : undefined,
                      }}
                    >
                      Deliveries
                    </button>
                    <button
                      onClick={() => handleDelete(sub.id)}
                      className="btn"
                      style={{ padding: "4px 8px", fontSize: 12, color: "var(--signal-red)" }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Deliveries History Drawer/Section */}
      {selectedSub && (
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, padding: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div>
              <h3 style={{ fontSize: 16, fontWeight: 600 }}>Delivery History Log</h3>
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
                Recent outbound attempts for <code style={{ color: "var(--signal-blue)" }}>{selectedSub.url}</code>
              </p>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn" onClick={() => loadDeliveries(selectedSub.id)} disabled={loadingDeliveries} style={{ fontSize: 12 }}>
                {loadingDeliveries ? "Refreshing…" : "Refresh Logs"}
              </button>
              <button className="btn" onClick={() => setSelectedSub(null)} style={{ fontSize: 12 }}>
                Close
              </button>
            </div>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Attempt</th>
                  <th>Delivered At</th>
                  <th>Response / Error</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.length === 0 && (
                  <tr>
                    <td colSpan={6} style={{ color: "var(--text-faint)", textAlign: "center", padding: 20 }}>
                      No delivery attempts recorded for this webhook yet. Click "Test Ping" above to trigger an attempt.
                    </td>
                  </tr>
                )}
                {deliveries.map((del) => (
                  <tr key={del.id}>
                    <td>
                      <span className="badge running" style={{ fontSize: 11 }}>{del.event}</span>
                    </td>
                    <td>
                      <span className={`badge ${del.is_success ? "completed" : "failed"}`}>
                        {del.status_code ? `HTTP ${del.status_code}` : del.is_success ? "SUCCESS" : "FAILED"}
                      </span>
                    </td>
                    <td style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>
                      {del.duration_ms !== null && del.duration_ms !== undefined ? `${del.duration_ms}ms` : "--"}
                    </td>
                    <td style={{ fontSize: 12, textAlign: "center" }}>#{del.attempt_number}</td>
                    <td style={{ fontSize: 12, color: "var(--text-dim)" }}>
                      {new Date(del.delivered_at).toLocaleTimeString()}
                    </td>
                    <td style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {del.error_message ? (
                        <span style={{ color: "var(--signal-red)", fontSize: 12 }} title={del.error_message}>
                          {del.error_message}
                        </span>
                      ) : del.response_body ? (
                        <code style={{ fontSize: 11, color: "var(--text-dim)" }} title={del.response_body}>
                          {del.response_body.slice(0, 100)}
                        </code>
                      ) : (
                        <span style={{ color: "var(--text-faint)", fontSize: 12 }}>--</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
