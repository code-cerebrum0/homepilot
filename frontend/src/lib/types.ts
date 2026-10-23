export type Role = "owner" | "admin" | "member";
export interface Membership { household_id: number; household_name: string; role: Role }
export interface Me { id: number; name: string; email: string; memberships: Membership[] }

export type Freshness = "live" | "recent" | "stale" | "none";
export type DeviceKind = "consumer" | "generator";

export interface DeviceUsage {
  device_id: number; name: string; kind: DeviceKind; kwh_today: number; share_pct: number;
  is_simulated: boolean; status: "active" | "disabled"; last_seen_at: string | null;
}
export interface Dashboard {
  as_of: string;
  last_reading_at: string | null;
  freshness: Freshness;
  current_power_w: number | null;
  today_kwh: number;
  yesterday_kwh: number;
  solar_today_kwh: number;
  solar_current_w: number | null;
  net_today_kwh: number;
  solar_daily: { date: string; kwh: number }[];
  cost: { today: number; month_to_date: number; currency: string; is_estimate: true; basis: string };
  month: { kwh_this_period: number; kwh_previous_period: number };
  devices: DeviceUsage[];
  daily: { date: string; kwh: number }[];
  opportunities: { title: string; detail: string; estimated_monthly_saving_kwh: number | null }[];
  has_simulated_data: boolean;
  has_solar: boolean;
}

export interface Device {
  id: number; name: string; household_id: number;
  kind: DeviceKind;
  source: "simulated" | "smart_plug" | "home_assistant" | "mqtt" | "local_api" | "solar_inverter";
  status: "active" | "disabled"; last_seen_at: string | null; created_at: string;
}
export interface DeviceCreated extends Device { ingestion_secret: string }
export interface Reading {
  id: number; device_id: number; measured_at: string; ingested_at: string;
  power_w: number; energy_kwh: number; interval_seconds: number; is_simulated: boolean;
}
export interface Page<T> { items: T[]; next_cursor: string | null }

export interface Tariff {
  id: number; name: string; currency: string; type: "flat" | "slab" | "time_of_use";
  flat_rate_per_kwh: number | null; fixed_monthly_charge: number; tax_percent: number;
  slabs: { up_to_kwh: number | null; rate_per_kwh: number }[] | null;
  tou_periods: { start: string; end: string; rate_per_kwh: number }[] | null;
}

export interface ToolCallInfo { name: string; status: "success" | "error"; summary: string }
export interface ChatReply { conversation_id: string; reply: string; tool_calls: ToolCallInfo[]; provider: "ollama" | "bedrock" }
