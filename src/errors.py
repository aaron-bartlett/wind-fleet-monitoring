"""Exception hierarchy for the Wind Fleet Monitor.

Every error raised deliberately by application code is a `WindFleetError` subclass, so
`app.py` can catch broadly at the UI boundary (`CLAUDE.md` §5.3) without swallowing bugs
expressed as other exception types.
"""


class WindFleetError(Exception):
    """Base class for all deliberately raised application errors."""


class DataLoadError(WindFleetError):
    """Raised when a source file is missing, malformed, or missing a required column."""


class QueryError(WindFleetError):
    """Raised when a DuckDB query fails or is given an invalid parameter."""


class ConfigError(WindFleetError):
    """Raised when an environment-derived setting or threshold is invalid."""


class NWPUnavailableError(WindFleetError):
    """Raised when an NWP provider cannot serve a request (e.g. out of domain)."""
