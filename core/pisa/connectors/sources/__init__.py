"""The source connectors' providers (a bank, a sport platform, a mailbox, the AdE). Each returns
typed data or raises `SourceError` naming the real cause, never a key, a token or a password."""

from pisa.connectors.sources.errors import SourceError

__all__ = ["SourceError"]
