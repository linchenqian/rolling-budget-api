class DomainError(Exception):
    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class ConflictError(DomainError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, status_code=409)


class NotFoundError(DomainError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, status_code=404)
