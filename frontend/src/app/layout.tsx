// @ts-expect-error Next.js handles this global stylesheet import at build time.
import "./globals.css";
import type { Metadata, Viewport } from "next";
import { AuthProvider } from "@/lib/auth";

export const metadata: Metadata = { title: "HomePilot", description: "Smart home energy monitoring with an AI assistant" };
export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><AuthProvider>{children}</AuthProvider></body></html>;
}




// "use client";
// import { useEffect } from "react";
// import Link from "next/link";
// import { usePathname, useRouter } from "next/navigation";
// import { useAuth } from "@/lib/auth";

// const links = [["/dashboard", "Dashboard"], ["/devices", "Devices"], ["/assistant", "Assistant"], ["/settings", "Settings"]];

// export default function AppLayout({ children }: { children: React.ReactNode }) {
//   const { me, loading } = useAuth(); const router = useRouter(); const path = usePathname();
//   useEffect(() => { if (!loading && !me) router.replace("/login"); }, [loading, me, router]);
//   if (loading || !me) return <div className="shell muted">Loading…</div>;
//   return (
//     <div className="shell">
//       <div className="topbar">
//         <div className="brand">🏠 HomePilot <span className="muted">· {me.memberships[0]?.household_name ?? "No household"}</span></div>
//         <nav className="nav">{links.map(([h, t]) => <Link key={h} href={h} className={path.startsWith(h) ? "on" : ""}>{t}</Link>)}</nav>
//       </div>
//       {children}
//     </div>
//   );
// }
