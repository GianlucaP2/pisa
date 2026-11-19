"""A source provider's failure: the real cause in plain words; `auth` when the credential was refused. Its
words go through `pisa.failures.redact` (a provider may echo a secret back)."""


class SourceError(Exception):
    def __init__(self, message: str, *, status: int | None = None, auth: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.auth = auth
