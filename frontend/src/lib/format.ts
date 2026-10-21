export const kwh = (v: number) => `${v.toFixed(2)} kWh`;
export const money = (v: number, cur: string) => new Intl.NumberFormat(undefined, { style: "currency", currency: cur }).format(v);
export const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "Never");
export function ago(iso: string | null): string {
  if (!iso) return "no data";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
