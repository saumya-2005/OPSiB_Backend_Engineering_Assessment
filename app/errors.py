"""Domain errors -> uniform API errors  {"error": <code>, "detail": <message>}."""


class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFound(DomainError):
    status, code = 404, "not_found"


class Forbidden(DomainError):
    status, code = 403, "forbidden"


class InvalidTransitionError(DomainError):
    status, code = 409, "invalid_transition"


class Conflict(DomainError):
    status, code = 409, "conflict"


class ValidationFailed(DomainError):
    status, code = 422, "validation_failed"


class IdempotencyKeyReuse(DomainError):
    status, code = 422, "idempotency_key_reuse"
