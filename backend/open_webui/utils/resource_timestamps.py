"""Shared timestamp normalization for resource visibility and events."""

# Event timestamps arrive from several producers: ORM model_dump uses ns,
# webhooks/clients may use seconds or milliseconds, and reaction/reply payloads
# sometimes carry no timestamp at all. Every comparison happens in ns, which
# is the unit of ResourceEntry.cutoff_at (time.time_ns()).
_EPOCH_SCALES = (
    (100_000_000_000, 1_000_000_000),  # seconds -> ns
    (100_000_000_000_000, 1_000_000),  # milliseconds -> ns
    (100_000_000_000_000_000, 1_000),  # microseconds -> ns
)


def normalize_created_at_ns(value):
    """Return an event timestamp in nanoseconds, or 0 when unusable.

    A missing or malformed value is reported as 0. It must not be treated as
    an old message (silently dropping every reaction/edit forever), nor as a
    guaranteed-new one (bypassing a real clear boundary). Callers resolve the
    actual message before pushing a body.
    """
    if isinstance(value, bool) or value is None:
        return 0
    fractional = None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0
        try:
            if any(c in value for c in '.eE'):
                as_float = float(value)
                fractional = as_float - int(as_float)
                value = int(as_float)
            else:
                value = int(value)
        except (ValueError, OverflowError):
            return 0
    if isinstance(value, float):
        fractional = value - int(value)
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        return 0
    for magnitude, scale in _EPOCH_SCALES:
        if value < magnitude:
            return value * scale + int((fractional or 0) * scale)
    return value
