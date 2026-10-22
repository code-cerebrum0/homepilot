import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DeviceSource = Literal["simulated", "smart_plug", "home_assistant", "mqtt", "local_api"]
DeviceStatus = Literal["active", "disabled"]


# ---------- auth / users ----------
class RegisterIn(BaseModel):
    name: Name
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class MembershipOut(BaseModel):
    household_id: int
    household_name: str
    role: str


class MeOut(BaseModel):
    id: int
    name: str
    email: str
    memberships: list[MembershipOut]


class UserPatchIn(BaseModel):
    name: Name


# ---------- devices ----------
class DeviceCreateIn(BaseModel):
    household_id: int
    name: Name
    source: DeviceSource


class DevicePatchIn(BaseModel):
    name: Name | None = None
    status: DeviceStatus | None = None

    @model_validator(mode="after")
    def _non_empty(self):
        if self.name is None and self.status is None:
            raise ValueError("Provide at least one of: name, status")
        return self


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    household_id: int
    name: str
    source: str
    status: str
    last_seen_at: datetime | None
    created_at: datetime


class DeviceCreatedOut(DeviceOut):
    ingestion_secret: str


class DeviceListOut(BaseModel):
    items: list[DeviceOut]
    next_cursor: str | None = None


class SecretOut(BaseModel):
    ingestion_secret: str


# ---------- readings ----------
class ReadingIn(BaseModel):
    device_id: int
    measured_at: AwareDatetime
    power_w: float = Field(ge=0, le=100_000)
    energy_kwh: float = Field(ge=0, le=1_000)
    interval_seconds: int = Field(gt=0, le=3600)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    measured_at: datetime
    ingested_at: datetime
    power_w: float
    energy_kwh: float
    interval_seconds: int
    is_simulated: bool


class ReadingListOut(BaseModel):
    items: list[ReadingOut]
    next_cursor: str | None


# ---------- tariff ----------
class Slab(BaseModel):
    up_to_kwh: float | None = Field(default=None, gt=0)
    rate_per_kwh: float = Field(ge=0)


class TouPeriod(BaseModel):
    start: str
    end: str
    rate_per_kwh: float = Field(ge=0)

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("time must be HH:MM (24h)")
        return v

    @model_validator(mode="after")
    def _distinct(self):
        if self.start == self.end:
            raise ValueError("start and end must differ")
        return self


class TariffIn(BaseModel):
    name: Name = "Home tariff"
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    type: Literal["flat", "slab", "time_of_use"]
    flat_rate_per_kwh: float | None = Field(default=None, ge=0)
    fixed_monthly_charge: float = Field(default=0, ge=0)
    tax_percent: float = Field(default=0, ge=0, le=100)
    slabs: list[Slab] | None = None
    tou_periods: list[TouPeriod] | None = None

    @model_validator(mode="after")
    def _check(self):
        self.currency = self.currency.upper()
        if self.type == "flat":
            if self.flat_rate_per_kwh is None:
                raise ValueError("flat_rate_per_kwh is required for a flat tariff")
            self.slabs = None
            self.tou_periods = None
        elif self.type == "slab":
            if not self.slabs:
                raise ValueError("slabs are required for a slab tariff")
            for i, s in enumerate(self.slabs):
                last = i == len(self.slabs) - 1
                if last and s.up_to_kwh is not None:
                    raise ValueError("the last slab must have up_to_kwh = null")
                if not last:
                    if s.up_to_kwh is None:
                        raise ValueError("only the last slab may have up_to_kwh = null")
                    prev = self.slabs[i - 1].up_to_kwh if i else 0
                    if s.up_to_kwh <= prev:
                        raise ValueError("slab limits must be strictly increasing")
            self.tou_periods = None
        else:
            if not self.tou_periods:
                raise ValueError("tou_periods are required for a time_of_use tariff")
            self.slabs = None
        return self


class TariffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    currency: str
    type: str
    flat_rate_per_kwh: float | None
    fixed_monthly_charge: float
    tax_percent: float
    slabs: list[Slab] | None
    tou_periods: list[TouPeriod] | None


# ---------- dashboard ----------
class CostOut(BaseModel):
    today: float
    month_to_date: float
    currency: str
    is_estimate: bool = True
    basis: str


class MonthOut(BaseModel):
    kwh_this_period: float
    kwh_previous_period: float


class DeviceUsageOut(BaseModel):
    device_id: int
    name: str
    kwh_today: float
    share_pct: float
    is_simulated: bool
    status: str
    last_seen_at: datetime | None


class DailyPoint(BaseModel):
    date: date
    kwh: float


class OpportunityOut(BaseModel):
    title: str
    detail: str
    estimated_monthly_saving_kwh: float | None


class DashboardOut(BaseModel):
    as_of: datetime
    last_reading_at: datetime | None
    freshness: Literal["live", "recent", "stale", "none"]
    current_power_w: float | None
    today_kwh: float
    yesterday_kwh: float
    cost: CostOut
    month: MonthOut
    devices: list[DeviceUsageOut]
    daily: list[DailyPoint]
    opportunities: list[OpportunityOut]
    has_simulated_data: bool


# ---------- chat ----------
class ChatIn(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    conversation_id: UUID | None = None


class ToolCallOut(BaseModel):
    name: str
    status: Literal["success", "error"]
    summary: str


class ChatOut(BaseModel):
    conversation_id: str
    reply: str
    tool_calls: list[ToolCallOut]
    provider: str = "ollama"
