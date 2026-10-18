"""Routing policy: PURE functions deciding who may receive a task. No DB, no clock.

Policy routing-v1 (per the brief)
---------------------------------
  tier 1 PRIMARY : receiving_associate at the SAME canonical location
  tier 2 FALLBACK: store_manager at the same location (the approved fallback)
  neither        : task stays unassigned and VISIBLE ("needs manual assignment")

A person is eligible only if ALL hold:
  1. employment_status == ACTIVE        <- always wins over shift data
  2. canonical location == task location (HRMS code was mapped via the crosswalk;
     region-scoped employees with no store are never auto-assigned a store task)
  3. a workforce snapshot exists, observed_at <= now and not older than max_age
  4. availability_status == ON_SHIFT and shift_start <= now <= shift_end

Safety invariant: an ineligible person is NEVER assigned, not even as a last resort.
Every decision records a `trace` (each candidate and why accepted/rejected).

To change who gets what, edit POLICIES (data, not logic): e.g. add a second tier.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

POLICY_VERSION = "routing-v1"


@dataclass(frozen=True)
class RoutingPolicy:
    primary_roles: tuple[str, ...]
    fallback_roles: tuple[str, ...]

    @property
    def all_roles(self) -> tuple[str, ...]:
        return self.primary_roles + self.fallback_roles


POLICIES: dict[str, RoutingPolicy] = {
    "RECEIPT_SHORTFALL": RoutingPolicy(("receiving_associate",), ("store_manager",)),
}


@dataclass(frozen=True)
class Candidate:
    employee_id: str
    role: str
    employment_status: str
    canonical_location_id: str | None
    availability_status: str | None      # latest snapshot at/before now, or None
    shift_start: datetime | None
    shift_end: datetime | None
    observed_at: datetime | None


@dataclass
class Decision:
    assignee_id: str | None
    tier: str                    # PRIMARY | FALLBACK | NONE
    reason: str
    trace: list[dict] = field(default_factory=list)


def rejection_reason(c: Candidate, location: str, now: datetime, max_age_minutes: int) -> str | None:
    """None = eligible, otherwise the (first) reason they are not."""
    if c.employment_status != "ACTIVE":
        return f"employment status {c.employment_status} (employment eligibility overrides shift data)"
    if c.canonical_location_id != location:
        return f"not at task location {location}"
    if c.availability_status is None or c.observed_at is None:
        return "no availability snapshot"
    if now - c.observed_at > timedelta(minutes=max_age_minutes):
        return f"availability snapshot stale ({c.observed_at.isoformat()})"
    if c.availability_status != "ON_SHIFT":
        return f"availability {c.availability_status}"
    if c.shift_start is None or c.shift_end is None or not (c.shift_start <= now <= c.shift_end):
        return "outside shift window"
    return None


def select_assignee(candidates: list[Candidate], policy: RoutingPolicy, location: str,
                    now: datetime, max_age_minutes: int) -> Decision:
    trace: list[dict] = []
    chosen: tuple[Candidate, str] | None = None
    for tier, roles in (("PRIMARY", policy.primary_roles), ("FALLBACK", policy.fallback_roles)):
        tier_candidates = sorted((c for c in candidates if c.role in roles), key=lambda c: c.employee_id)
        for c in tier_candidates:
            why = rejection_reason(c, location, now, max_age_minutes)
            trace.append({"employee_id": c.employee_id, "role": c.role, "tier": tier,
                          "eligible": why is None, "reason": why or "eligible"})
            if why is None and chosen is None:
                chosen = (c, tier)
        if chosen:                      # lower tiers are only consulted when the higher one is empty
            break
    if chosen is None:
        return Decision(None, "NONE",
                        f"no eligible {'/'.join(policy.primary_roles)} or fallback "
                        f"{'/'.join(policy.fallback_roles)} at {location}; manual assignment required", trace)
    c, tier = chosen
    rejected = [f"{t['employee_id']} ({t['reason']})" for t in trace if not t["eligible"]]
    reason = f"{c.employee_id} selected as {tier} {c.role} at {location}: ACTIVE, ON_SHIFT, fresh availability"
    if tier == "FALLBACK":
        reason += "; no eligible " + "/".join(policy.primary_roles) + " was available"
    if rejected:
        reason += ". Not selected: " + "; ".join(rejected)
    return Decision(c.employee_id, tier, reason, trace)
