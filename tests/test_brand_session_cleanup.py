"""Tests voor CR-K3 — Brand session cleanup.

Verifieert dat cleanup_stale_sessions() oude sessies verwijdert,
recente behoudt, en correct omgaat met ontbrekende metadata.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from openaec_reports.brand_api import (
    cleanup_stale_sessions,
)


@pytest.fixture()
def _clean_sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect SESSIONS_DIR naar een tijdelijke directory."""
    import openaec_reports.brand_api as brand_api

    monkeypatch.setattr(brand_api, "SESSIONS_DIR", tmp_path)
    yield tmp_path


def _create_session(
    sessions_dir: Path,
    session_id: str,
    created_at: float | None = None,
    write_metadata: bool = True,
) -> Path:
    """Helper: maak een fake sessie aan.

    Args:
        sessions_dir: Root directory voor sessies.
        session_id: Sessie ID (12 hex chars).
        created_at: Timestamp voor metadata (None = nu).
        write_metadata: Of session.json geschreven moet worden.

    Returns:
        Pad naar de sessie directory.
    """
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "uploads").mkdir(exist_ok=True)
    (session_dir / "output").mkdir(exist_ok=True)

    if write_metadata:
        meta = {
            "brand_name": "Test Brand",
            "brand_slug": "test-brand",
            "owner_id": "test-user",
            "created_at": created_at if created_at is not None else time.time(),
        }
        (session_dir / "session.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )

    return session_dir


class TestCleanupStaleSessions:
    """Tests voor cleanup_stale_sessions()."""

    def test_removes_old_session(self, _clean_sessions_dir: Path) -> None:
        """Sessie ouder dan max_age wordt verwijderd."""
        old_time = time.time() - (25 * 3600)  # 25 uur geleden
        session_dir = _create_session(
            _clean_sessions_dir, "aabbccddeeff", created_at=old_time
        )

        removed = cleanup_stale_sessions(max_age_hours=24.0)

        assert removed == 1
        assert not session_dir.exists()

    def test_keeps_recent_session(self, _clean_sessions_dir: Path) -> None:
        """Recente sessie wordt NIET verwijderd."""
        recent_time = time.time() - (1 * 3600)  # 1 uur geleden
        session_dir = _create_session(
            _clean_sessions_dir, "112233445566", created_at=recent_time
        )

        removed = cleanup_stale_sessions(max_age_hours=24.0)

        assert removed == 0
        assert session_dir.exists()

    def test_mixed_old_and_new(self, _clean_sessions_dir: Path) -> None:
        """Alleen oude sessies worden verwijderd, recente blijven."""
        old_time = time.time() - (48 * 3600)
        recent_time = time.time() - (2 * 3600)

        old_dir = _create_session(
            _clean_sessions_dir, "aaaaaaaaaaaa", created_at=old_time
        )
        new_dir = _create_session(
            _clean_sessions_dir, "bbbbbbbbbbbb", created_at=recent_time
        )

        removed = cleanup_stale_sessions(max_age_hours=24.0)

        assert removed == 1
        assert not old_dir.exists()
        assert new_dir.exists()

    def test_fallback_to_mtime_without_metadata(
        self, _clean_sessions_dir: Path
    ) -> None:
        """Zonder session.json wordt directory mtime gebruikt."""
        session_dir = _create_session(
            _clean_sessions_dir, "cccccccccccc", write_metadata=False
        )

        # Directory is zojuist aangemaakt, dus mtime is nu → niet verwijderd
        removed = cleanup_stale_sessions(max_age_hours=24.0)
        assert removed == 0
        assert session_dir.exists()

    def test_empty_sessions_dir(self, _clean_sessions_dir: Path) -> None:
        """Lege sessions directory geeft 0 verwijderd."""
        removed = cleanup_stale_sessions(max_age_hours=24.0)
        assert removed == 0

    def test_nonexistent_sessions_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Als SESSIONS_DIR niet bestaat, geeft het 0 terug."""
        import openaec_reports.brand_api as brand_api

        monkeypatch.setattr(
            brand_api, "SESSIONS_DIR", tmp_path / "does_not_exist"
        )
        removed = cleanup_stale_sessions(max_age_hours=24.0)
        assert removed == 0

    def test_custom_max_age(self, _clean_sessions_dir: Path) -> None:
        """Custom max_age_hours wordt gerespecteerd."""
        # 2 uur oud, met max_age=1 uur → moet verwijderd worden
        old_time = time.time() - (2 * 3600)
        session_dir = _create_session(
            _clean_sessions_dir, "dddddddddddd", created_at=old_time
        )

        removed = cleanup_stale_sessions(max_age_hours=1.0)

        assert removed == 1
        assert not session_dir.exists()

    def test_ignores_non_hex_directories(
        self, _clean_sessions_dir: Path
    ) -> None:
        """Directories die niet aan het hex pattern voldoen worden overgeslagen."""
        non_hex_dir = _clean_sessions_dir / "not-a-session"
        non_hex_dir.mkdir()

        removed = cleanup_stale_sessions(max_age_hours=0.0)
        assert removed == 0
        assert non_hex_dir.exists()
