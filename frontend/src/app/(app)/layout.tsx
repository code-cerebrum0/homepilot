"use client";
import { useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

const links = [["/dashboard", "Dashboard"], ["/devices", "Devices"], ["/assistant", "Assistant"], ["/settings", "Settings"]];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { me, loading } = useAuth(); const router = useRouter(); const path = usePathname();
  useEffect(() => { if (!loading && !me) router.replace("/login"); }, [loading, me, router]);
  if (loading || !me) return <div className="shell muted">Loading…</div>;
  return (
    <div className="shell">
      <div className="topbar">
        <div className="brand">🏠 HomePilot <span className="muted">· {me.memberships[0]?.household_name ?? "No household"}</span></div>
        <nav className="nav">{links.map(([h, t]) => <Link key={h} href={h} className={path.startsWith(h) ? "on" : ""}>{t}</Link>)}</nav>
      </div>
      {children}
    </div>
  );
}
