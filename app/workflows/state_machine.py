"""Server-enforced task state machine: PURE, no DB. The client can never set a status;
it can only request an ACTION, and this table decides whether that action is legal.

CREATED ──assign──▶ ASSIGNED ──acknowledge──▶ ACKNOWLEDGED ──start──▶ IN_PROGRESS
                       ▲  ▲                                              │  │  ▲
                       │  └────────────── reassign (from any open state) ┘  │  │
                       │                                          block ▼    │  │ unblock
                       │                                           BLOCKED ──┘  │
                       │                                                         │ complete
        IN_PROGRESS ◀── reject ── PENDING_VERIFICATION ◀── (auto) ── COMPLETED ◀─┘
                                      │ verify
                                      ▼
                                   VERIFIED  (terminal; resolves the exception)

COMPLETED is a transient state: completing a task immediately moves it on to
PENDING_VERIFICATION in the same transaction (two audit events). "Completed" never
means "resolved": only a verification by a DIFFERENT authorised person does that.
"""
from enum import Enum


class Status(str, Enum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"


class Action(str, Enum):
    ASSIGN = "ASSIGN"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    START = "START"
    BLOCK = "BLOCK"
    UNBLOCK = "UNBLOCK"
    COMPLETE = "COMPLETE"
    SUBMIT_FOR_VERIFICATION = "SUBMIT_FOR_VERIFICATION"
    VERIFY = "VERIFY"
    REJECT = "REJECT"
    REASSIGN = "REASSIGN"


S, A = Status, Action
TRANSITIONS: dict[tuple[Status, Action], Status] = {
    (S.CREATED, A.ASSIGN): S.ASSIGNED,
    (S.CREATED, A.REASSIGN): S.ASSIGNED,          # manual assignment of an unassigned task
    (S.ASSIGNED, A.ACKNOWLEDGE): S.ACKNOWLEDGED,
    (S.ASSIGNED, A.REASSIGN): S.ASSIGNED,
    (S.ACKNOWLEDGED, A.START): S.IN_PROGRESS,
    (S.ACKNOWLEDGED, A.REASSIGN): S.ASSIGNED,
    (S.IN_PROGRESS, A.BLOCK): S.BLOCKED,
    (S.IN_PROGRESS, A.COMPLETE): S.COMPLETED,
    (S.IN_PROGRESS, A.REASSIGN): S.ASSIGNED,
    (S.BLOCKED, A.UNBLOCK): S.IN_PROGRESS,
    (S.BLOCKED, A.REASSIGN): S.ASSIGNED,
    (S.COMPLETED, A.SUBMIT_FOR_VERIFICATION): S.PENDING_VERIFICATION,
    (S.PENDING_VERIFICATION, A.VERIFY): S.VERIFIED,
    (S.PENDING_VERIFICATION, A.REJECT): S.IN_PROGRESS,   # reopened for more work
}

# Retried requests: if the task is ALREADY in one of these states, the same actor repeating
# the action is a harmless no-op (the first attempt succeeded, the response was lost).
REPLAY_OK: dict[Action, frozenset[Status]] = {
    A.ACKNOWLEDGE: frozenset({S.ACKNOWLEDGED}),
    A.START: frozenset({S.IN_PROGRESS}),
    A.BLOCK: frozenset({S.BLOCKED}),
    A.COMPLETE: frozenset({S.PENDING_VERIFICATION, S.VERIFIED}),
    A.VERIFY: frozenset({S.VERIFIED}),
}


class InvalidTransition(Exception):
    def __init__(self, status: Status, action: Action):
        super().__init__(f"action {action.value} is not allowed while task is {status.value}")
        self.status, self.action = status, action


def next_status(status: Status, action: Action) -> Status:
    try:
        return TRANSITIONS[(status, action)]
    except KeyError:
        raise InvalidTransition(status, action) from None
