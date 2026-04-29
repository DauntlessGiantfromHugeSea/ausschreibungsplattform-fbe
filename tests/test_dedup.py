from datetime import datetime

from backend.dedup import fingerprint


def test_fingerprint_stable():
    a = fingerprint("https://example.org/x", "Titel A", "Stadt Foo", datetime(2026, 5, 1))
    b = fingerprint("https://example.org/x", "Titel A", "Stadt Foo", datetime(2026, 5, 1))
    assert a == b


def test_fingerprint_differs_by_url():
    a = fingerprint("https://example.org/a", "Titel", "Stadt", None)
    b = fingerprint("https://example.org/b", "Titel", "Stadt", None)
    assert a != b


def test_fingerprint_normalizes_whitespace_and_case():
    a = fingerprint("HTTPS://example.org/X ", "  Titel A ", "Stadt FOO", None)
    b = fingerprint("https://example.org/X", "titel a", "stadt foo", None)
    assert a == b


def test_fingerprint_includes_deadline_date_only():
    a = fingerprint("u", "t", "x", datetime(2026, 5, 1, 9, 0))
    b = fingerprint("u", "t", "x", datetime(2026, 5, 1, 17, 30))
    assert a == b  # gleiche Kalenderdaten → gleicher Fingerprint
    c = fingerprint("u", "t", "x", datetime(2026, 5, 2))
    assert a != c
