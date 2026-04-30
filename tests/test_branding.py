"""Tests fuer das Branding-Modul (Logo-Cache)."""
from __future__ import annotations

from unittest.mock import patch

from backend import branding


def test_has_logo_when_present(tmp_path, monkeypatch):
    fake_logo = tmp_path / "logo.png"
    fake_logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    monkeypatch.setattr(branding, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(branding, "LOGO_FILE", fake_logo)
    monkeypatch.setattr(branding, "FAVICON_FILE", tmp_path / "favicon.png")
    assert branding.has_logo() is True


def test_has_logo_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(branding, "LOGO_FILE", tmp_path / "missing.png")
    monkeypatch.setattr(branding, "FAVICON_FILE", tmp_path / "missing-favicon.png")
    assert branding.has_logo() is False


def test_ensure_logo_skips_when_already_cached(tmp_path, monkeypatch):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    monkeypatch.setattr(branding, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(branding, "LOGO_FILE", logo)
    monkeypatch.setattr(branding, "FAVICON_FILE", tmp_path / "favicon.png")

    with patch("backend.branding.httpx.Client") as mock_client:
        result = branding.ensure_logo()

    assert result is True
    mock_client.assert_not_called()
    assert (tmp_path / "favicon.png").exists()


def test_ensure_logo_handles_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(branding, "LOGO_FILE", tmp_path / "logo.png")
    monkeypatch.setattr(branding, "FAVICON_FILE", tmp_path / "favicon.png")

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            raise RuntimeError("network down")

    monkeypatch.setattr(branding.httpx, "Client", FakeClient)
    assert branding.ensure_logo() is False
    assert not (tmp_path / "logo.png").exists()


def test_ensure_logo_handles_http_404(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(branding, "LOGO_FILE", tmp_path / "logo.png")
    monkeypatch.setattr(branding, "FAVICON_FILE", tmp_path / "favicon.png")

    class FakeResp:
        status_code = 404
        content = b""

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return FakeResp()

    monkeypatch.setattr(branding.httpx, "Client", FakeClient)
    assert branding.ensure_logo() is False


def test_ensure_logo_downloads_and_writes_favicon(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(branding, "LOGO_FILE", tmp_path / "logo.png")
    monkeypatch.setattr(branding, "FAVICON_FILE", tmp_path / "favicon.png")

    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 500

    class FakeResp:
        status_code = 200
        content = payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return FakeResp()

    monkeypatch.setattr(branding.httpx, "Client", FakeClient)

    assert branding.ensure_logo() is True
    assert (tmp_path / "logo.png").read_bytes() == payload
    assert (tmp_path / "favicon.png").read_bytes() == payload
