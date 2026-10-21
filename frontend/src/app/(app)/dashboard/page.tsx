"use client";
import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ago, kwh, money, when } from "@/lib/format";
import type { Dashboard } from "@/lib/types";

const FRESH = { live: ["live", ""], recent: ["recent", ""], stale: ["stale data", "warn"], none: ["no data", "off"] } as const;

export default function DashboardPage() {
  const { householdId } = useAuth();
  const [days, setDays] = useState(7);
  const [d, setD] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!householdId) return;
    let alive = true;
    const load = () => api<Dashboard>(`/api/v1/households/${householdId}/dashboard?days=${days}`)
      .then(r => { if (alive) { setD(r); setError(""); } })
      .catch(e => alive && setError(e.message));
    load(); const t = setInterval(load, 30000);
    return () => { alive = false; clearInterval(t); };
  }, [householdId, days]);

  if (!householdId) return <div className="card">You are not a member of any household yet.</div>;
  if (error && !d) return <div className="card err">{error}</div>;
  if (!d) return <div className="card muted">Loading dashboard…</div>;

  const [label, cls] = FRESH[d.freshness];
  const delta = d.month.kwh_previous_period > 0 ? ((d.month.kwh_this_period - d.month.kwh_previous_period) / d.month.kwh_previous_period) * 100 : null;
  const top = d.devices[0];

  return (
    <>
      <div className="card hero">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
          <span className={`badge ${cls}`}>{label}</span>
          <span className="muted">Last reading: {ago(d.last_reading_at)}</span>
        </div>
        {d.has_simulated_data && <span className="badge sim">Includes simulated readings</span>}
        <div className="hero-grid" style={{ marginTop: 12 }}>
          <div className="stat"><p>Power now</p><b>{d.current_power_w === null ? "—" : (d.current_power_w / 1000).toFixed(2)}</b><small>kW</small></div>
          <House />
          <div className="stat"><p>Today</p><b>{d.today_kwh.toFixed(2)}</b><small>kWh</small></div>
          <div className="stat"><p>Estimated cost today</p><b>{money(d.cost.today, d.cost.currency)}</b></div>
          <div className="stat"><p>Top device today</p><b style={{ fontSize: 18 }}>{top ? top.name : "—"}</b></div>
          <div className="stat"><p>Yesterday</p><b>{d.yesterday_kwh.toFixed(2)}</b><small>kWh</small></div>
        </div>
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Overview</h2>
          <div className="row" style={{ gap: 28 }}>
            <div className="stat"><b style={{ color: "var(--teal)" }}>{d.month.kwh_this_period.toFixed(1)}</b><small>kWh this month</small></div>
            <div className="stat"><b style={{ color: "var(--orange)" }}>{money(d.cost.month_to_date, d.cost.currency)}</b><small>estimated bill so far</small></div>
          </div>
          <p className="muted">
            {delta === null ? "No previous-period data to compare." : `${Math.abs(delta).toFixed(0)}% ${delta >= 0 ? "higher" : "lower"} than the same period last month (${kwh(d.month.kwh_previous_period)}).`}
          </p>
          <p className="muted">Estimate basis: {d.cost.basis}. This is not an exact utility bill.</p>
        </div>
        <div className="card">
          <h2>Saving opportunities</h2>
          {d.opportunities.length === 0 && <p className="muted">No opportunities detected from your current data.</p>}
          {d.opportunities.map((o, i) => (
            <div key={i} style={{ marginBottom: 10 }}>
              <b>{o.title}</b>
              <div className="muted">{o.detail}{o.estimated_monthly_saving_kwh !== null && ` (≈ ${o.estimated_monthly_saving_kwh.toFixed(1)} kWh/month)`}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>Daily usage</h2>
          <select style={{ width: "auto" }} value={days} onChange={e => setDays(Number(e.target.value))}>
            <option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
          </select>
        </div>
        {d.daily.length === 0 ? <p className="muted">No readings in this period.</p> : (
          <div style={{ height: 240 }}>
            <ResponsiveContainer>
              <BarChart data={d.daily}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" tick={{ fontSize: 12 }} />
                <YAxis tick={{ fontSize: 12 }} unit=" kWh" width={70} />
                <Tooltip formatter={(v: number) => [`${v.toFixed(2)} kWh`, "Usage"]} />
                <Bar dataKey="kwh" fill="#13b98f" radius={[6, 6, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Device consumption today</h2>
        {d.devices.length === 0 && <p className="muted">No devices registered yet.</p>}
        {d.devices.map(v => (
          <div key={v.device_id} style={{ marginBottom: 14 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span><b>{v.name}</b> {v.is_simulated && <span className="badge sim">simulated</span>} {v.status === "disabled" && <span className="badge off">disabled</span>}</span>
              <span>{kwh(v.kwh_today)} · {v.share_pct.toFixed(0)}%</span>
            </div>
            <div className="bar"><i style={{ width: `${Math.min(100, v.share_pct)}%` }} /></div>
            <div className="muted" style={{ fontSize: 12 }}>Last seen {when(v.last_seen_at)}</div>
          </div>
        ))}
      </div>
    </>
  );
}

function House() {
  return (
    <svg viewBox="0 0 200 130" width="100%" style={{ maxHeight: 150 }} aria-hidden>
      <polygon points="20,60 100,10 180,60" fill="#13b98f" opacity=".85" />
      <rect x="35" y="60" width="130" height="60" rx="4" fill="#f4f6fa" stroke="#d5dbe6" />
      <rect x="50" y="72" width="26" height="48" fill="#f7b84b" opacity=".85" />
      <rect x="95" y="75" width="22" height="22" fill="#cfe7f7" /><rect x="127" y="75" width="22" height="22" fill="#cfe7f7" />
    </svg>
  );
}
