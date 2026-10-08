"""Kubernetes resource quantities as integers.

CPU is millicores and memory is bytes, so every comparison in rules.py is an
integer comparison. Floats were the first version and 0.1 + 0.2 showed up in a
quota message.
"""

from __future__ import annotations

import re

_MEMORY_SUFFIXES = {
    "": 1,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
}

_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")
_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)([a-zA-Z]{0,2})$")


class QuantityError(ValueError):
    """A quantity string Kubernetes would also refuse."""


def cpu_millis(value: str | int | float) -> int:
    """Return CPU as whole millicores. `500m` and `0.5` are both 500."""
    text = str(value).strip()
    match = _CPU_RE.match(text)
    if not match:
        raise QuantityError(f"not a CPU quantity: {value!r}")
    number, suffix = match.groups()
    millis = float(number) if suffix == "m" else float(number) * 1000
    if millis != int(millis):
        # Kubernetes rounds a sub-millicore request up, and a request that
        # needs rounding is a request nobody chose.
        raise QuantityError(f"CPU quantity {value!r} is finer than one millicore")
    return int(millis)


def memory_bytes(value: str | int | float) -> int:
    """Return memory as whole bytes. Binary and decimal suffixes both work."""
    text = str(value).strip()
    match = _MEMORY_RE.match(text)
    if not match:
        raise QuantityError(f"not a memory quantity: {value!r}")
    number, suffix = match.groups()
    if suffix not in _MEMORY_SUFFIXES:
        raise QuantityError(f"unknown memory suffix {suffix!r} in {value!r}")
    total = float(number) * _MEMORY_SUFFIXES[suffix]
    if total != int(total):
        raise QuantityError(f"memory quantity {value!r} is not a whole number of bytes")
    return int(total)


def format_cpu(millis: int) -> str:
    """Millicores back to the shortest form a human reads."""
    if millis % 1000 == 0:
        return str(millis // 1000)
    return f"{millis}m"


def format_memory(count: int) -> str:
    """Bytes back to the largest binary suffix that divides exactly."""
    for suffix in ("Ti", "Gi", "Mi", "Ki"):
        unit = _MEMORY_SUFFIXES[suffix]
        if count >= unit and count % unit == 0:
            return f"{count // unit}{suffix}"
    return str(count)


def format_cores(millis: int) -> str:
    """Millicores as cores, for a column of them. `8100m` reads as 8.10."""
    return f"{millis / 1000:.2f}"


def percent_of(part: int, whole: int) -> float:
    """part as a percentage of whole, 0.0 when whole is 0."""
    if whole == 0:
        return 0.0
    return 100.0 * part / whole
