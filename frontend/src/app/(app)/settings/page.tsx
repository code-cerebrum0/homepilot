"use client";
import { useEffect, useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { api, patch, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Tariff } from "@/lib/types";

export default function Settings() {
  const { me, householdId, reload, logout } = useAuth(); const router = useRouter();
  const canEdit = me?.memberships[0]?.role !== "member";
  const [name, setName] = useState(me?.name ?? "");
  const [t, setT] = useState<Tariff | null>(null);
  const [msg, setMsg] = useState(""); const [err, setErr] = useState("");

  useEffect(() => { if (householdId) api<Tariff>(`/api/v1/households/${householdId}/tariff`).then(setT).catch(() => setT(null)); }, [householdId]);

  async function run(fn: () => Promise<void>, ok: string) { setMsg(""); setErr(""); try { await fn(); setMsg(ok); } catch (e) { setErr((e as Error).message); } }
  const saveProfile = (e: FormEvent) => { e.preventDefault(); run(async () => { await patch("/api/v1/users/me", { name }); await reload(); }, "Profile saved."); };
  const saveTariff = (e: FormEvent) => { e.preventDefault(); if (t && householdId) run(async () => { setT(await put<Tariff>(`/api/v1/households/${householdId}/tariff`, t)); }, "Tariff saved."); };
  const num = (v: string) => (v === "" ? 0 : Number(v));

  return (
    <>
      {msg && <div className="card" style={{ color: "var(--teal)" }}>{msg}</div>}
      {err && <div className="card err">{err}</div>}
      <form className="card" onSubmit={saveProfile}>
        <h2>Profile</h2>
        <label>Name</label><input required maxLength={100} value={name} onChange={e => setName(e.target.value)} />
        <label>Email</label><input value={me?.email ?? ""} disabled />
        <div className="row" style={{ marginTop: 12 }}><button>Save</button></div>
      </form>

      <form className="card" onSubmit={saveTariff}>
        <h2>Electricity tariff</h2>
        {!t ? <p className="muted">No tariff configured. Cost estimates are unavailable until one is set by an owner or admin.</p> : (
          <>
            <div className="grid g2">
              <div><label>Currency (ISO code)</label><input maxLength={3} disabled={!canEdit} value={t.currency} onChange={e => setT({ ...t, currency: e.target.value.toUpperCase() })} /></div>
              <div><label>Rate per kWh (flat)</label><input type="number" step="0.01" min="0" disabled={!canEdit} value={t.flat_rate_per_kwh ?? ""} onChange={e => setT({ ...t, type: "flat", flat_rate_per_kwh: num(e.target.value) })} /></div>
              <div><label>Fixed monthly charge</label><input type="number" step="0.01" min="0" disabled={!canEdit} value={t.fixed_monthly_charge} onChange={e => setT({ ...t, fixed_monthly_charge: num(e.target.value) })} /></div>
              <div><label>Tax (%)</label><input type="number" step="0.01" min="0" max="100" disabled={!canEdit} value={t.tax_percent} onChange={e => setT({ ...t, tax_percent: num(e.target.value) })} /></div>
            </div>
            <p className="muted">Tariff type: {t.type}. Slab and time-of-use tariffs are editable through the API; this form edits flat-rate tariffs.</p>
            {canEdit && <button>Save tariff</button>}
          </>
        )}
      </form>

      <div className="card">
        <h2>Session</h2>
        <div className="row">
          <button className="ghost" onClick={async () => { await logout(); router.replace("/login"); }}>Log out</button>
          <button className="danger" onClick={async () => { await post("/api/v1/auth/logout-all"); await logout(); router.replace("/login"); }}>Log out of all devices</button>
        </div>
      </div>
    </>
  );
}
