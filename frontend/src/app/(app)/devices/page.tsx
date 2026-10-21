"use client";
import { Fragment, useCallback, useEffect, useState, FormEvent } from "react";
import { api, del, patch, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ago, when } from "@/lib/format";
import type { Device, DeviceCreated, Page, Reading } from "@/lib/types";

const SOURCES: Device["source"][] = ["simulated", "smart_plug", "home_assistant", "mqtt", "local_api"];

export default function Devices() {
  const { me, householdId } = useAuth();
  const role = me?.memberships[0]?.role;
  const canManage = role === "owner" || role === "admin";
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState("");
  const [name, setName] = useState(""); const [source, setSource] = useState<Device["source"]>("simulated");
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [readings, setReadings] = useState<Reading[]>([]);

  const load = useCallback(async () => {
    if (!householdId) return;
    try { setDevices((await api<Page<Device>>(`/api/v1/devices?household_id=${householdId}`)).items); }
    catch (e) { setError((e as Error).message); }
  }, [householdId]);
  useEffect(() => { load(); }, [load]);

  async function run(fn: () => Promise<void>) { setError(""); try { await fn(); await load(); } catch (e) { setError((e as Error).message); } }

  const add = (e: FormEvent) => { e.preventDefault(); run(async () => {
    const d = await post<DeviceCreated>("/api/v1/devices", { household_id: householdId, name, source });
    setSecret({ name: d.name, value: d.ingestion_secret }); setName("");
  }); };

  async function toggleReadings(id: number) {
    if (open === id) return setOpen(null);
    setOpen(id); setReadings([]);
    try { setReadings((await api<Page<Reading>>(`/api/v1/readings?device_id=${id}&limit=10`)).items); } catch (e) { setError((e as Error).message); }
  }

  return (
    <>
      {error && <div className="card err">{error}</div>}
      {secret && (
        <div className="secret">
          <b>Ingestion secret for “{secret.name}” — shown only once. Copy it now.</b><br />{secret.value}
          <div><button className="ghost" onClick={() => setSecret(null)} style={{ marginTop: 8 }}>I saved it</button></div>
        </div>
      )}
      {canManage && (
        <form className="card" onSubmit={add}>
          <h2>Register a device</h2>
          <div className="row">
            <input style={{ flex: 2, minWidth: 160 }} required maxLength={100} placeholder="Device name (e.g. Living room AC)" value={name} onChange={e => setName(e.target.value)} />
            <select style={{ flex: 1, minWidth: 140 }} value={source} onChange={e => setSource(e.target.value as Device["source"])}>
              {SOURCES.map(s => <option key={s} value={s}>{s.replace("_", " ")}</option>)}
            </select>
            <button>Add</button>
          </div>
        </form>
      )}
      <div className="card" style={{ overflowX: "auto" }}>
        <h2>Devices</h2>
        {devices.length === 0 ? <p className="muted">No devices yet.</p> : (
          <table>
            <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Last seen</th><th></th></tr></thead>
            <tbody>
              {devices.map(d => (
                <Fragment key={d.id}>
                  <tr>
                    <td><b>{d.name}</b></td>
                    <td>{d.source === "simulated" ? <span className="badge sim">simulated</span> : d.source.replace("_", " ")}</td>
                    <td><span className={`badge ${d.status === "active" ? "" : "off"}`}>{d.status}</span></td>
                    <td>{ago(d.last_seen_at)}</td>
                    <td><div className="row">
                      <button className="ghost" onClick={() => toggleReadings(d.id)}>Readings</button>
                      {canManage && <>
                        <button className="ghost" onClick={() => { const n = prompt("New name", d.name); if (n?.trim()) run(() => patch(`/api/v1/devices/${d.id}`, { name: n.trim() })); }}>Rename</button>
                        <button className="ghost" onClick={() => run(() => patch(`/api/v1/devices/${d.id}`, { status: d.status === "active" ? "disabled" : "active" }))}>{d.status === "active" ? "Disable" : "Enable"}</button>
                        <button className="ghost" onClick={() => confirm("Rotate secret? The old one stops working.") && run(async () => { const r = await post<{ ingestion_secret: string }>(`/api/v1/devices/${d.id}/rotate-secret`); setSecret({ name: d.name, value: r.ingestion_secret }); })}>Rotate key</button>
                        <button className="danger" onClick={() => confirm(`Remove “${d.name}”?`) && run(() => del(`/api/v1/devices/${d.id}`))}>Remove</button>
                      </>}
                    </div></td>
                  </tr>
                  {open === d.id && (
                    <tr><td colSpan={5}>
                      {readings.length === 0 ? <span className="muted">No readings found.</span> : (
                        <table>
                          <thead><tr><th>Measured</th><th>Power (W)</th><th>Energy (kWh)</th><th>Interval (s)</th></tr></thead>
                          <tbody>{readings.map(r => <tr key={r.id}><td>{when(r.measured_at)}</td><td>{r.power_w}</td><td>{r.energy_kwh}</td><td>{r.interval_seconds}</td></tr>)}</tbody>
                        </table>
                      )}
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
