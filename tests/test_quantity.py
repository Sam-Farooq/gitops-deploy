import pytest

from gitopsvalidate.quantity import (
    QuantityError,
    cpu_millis,
    format_cores,
    format_cpu,
    format_memory,
    memory_bytes,
    percent_of,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("500m", 500), ("1", 1000), ("2", 2000), ("0.5", 500), ("1.5", 1500), ("100m", 100)],
)
def test_cpu_millis(text, expected):
    assert cpu_millis(text) == expected


def test_cpu_rejects_sub_millicore():
    # Kubernetes rounds 0.0005 up to 1m. A request that needs rounding is a
    # request nobody chose, so it is refused rather than rounded here.
    with pytest.raises(QuantityError, match="finer than one millicore"):
        cpu_millis("0.0005")


@pytest.mark.parametrize("text", ["500M", "half", "", "1.5.2", "m100"])
def test_cpu_rejects_nonsense(text):
    with pytest.raises(QuantityError):
        cpu_millis(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512Mi", 512 * 1024**2),
        ("2Gi", 2 * 1024**3),
        ("1Ki", 1024),
        ("1000", 1000),
        ("1M", 1000**2),
        ("1G", 1000**3),
    ],
)
def test_memory_bytes(text, expected):
    assert memory_bytes(text) == expected


def test_binary_and_decimal_suffixes_differ():
    # The commonest resource mistake in a values file: 1G is 7% smaller than
    # 1Gi, and a 3G request against a 3Gi limit is Burstable, not Guaranteed.
    assert memory_bytes("1G") < memory_bytes("1Gi")
    assert memory_bytes("1Gi") - memory_bytes("1G") == 73741824


@pytest.mark.parametrize("text", ["512MB", "lots", "4Pi", "-1Mi"])
def test_memory_rejects_nonsense(text):
    with pytest.raises(QuantityError):
        memory_bytes(text)


def test_format_cpu_round_trips():
    for text in ("500m", "2", "1500m", "100m"):
        assert format_cpu(cpu_millis(text)) == text


def test_format_memory_picks_the_largest_exact_suffix():
    assert format_memory(memory_bytes("2048Mi")) == "2Gi"
    assert format_memory(memory_bytes("1536Mi")) == "1536Mi"


def test_format_memory_falls_back_to_bytes():
    assert format_memory(1000) == "1000"


def test_format_cores():
    assert format_cores(8100) == "8.10"
    assert format_cores(29200) == "29.20"


def test_percent_of_handles_zero():
    assert percent_of(5, 0) == 0.0
    assert percent_of(22600, 29200) == pytest.approx(77.397, abs=0.01)
