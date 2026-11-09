"""Test /api/generate/template endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
TENANTS_DIR = PROJECT_ROOT / "tenants"
TEST_JSON = PROJECT_ROOT / "schemas" / "test_336_bic_factuur.json"

SKIP = pytest.mark.skipif(
    not TENANTS_DIR.exists() or not TEST_JSON.exists(),
    reason="Tenant dir of test JSON niet aanwezig",
)


@SKIP
class TestApiTemplateEndpoint:
    """Test het /api/generate/template endpoint."""

    @pytest.mark.skipif(
        not (TENANTS_DIR / "customer" / "templates" / "bic_factuur.yaml").exists(),
        reason="Prive-tenant 'customer' (bic_factuur) niet aanwezig; staat niet in git",
    )
    def test_generate_customer_bic(self, authenticated_client) -> None:
        """Genereer Customer BIC factuur via API."""
        with TEST_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)

        response = authenticated_client.post(
            "/api/generate/template",
            json=data,
        )

        assert response.status_code == 200, f"API error: {response.text}"
        assert response.headers["content-type"] == "application/pdf"
        assert len(response.content) > 1000, "PDF te klein"

    def test_missing_template_field(self, authenticated_client) -> None:
        """Verifieer 422 bij ontbrekend template veld."""
        response = authenticated_client.post(
            "/api/generate/template",
            json={"project": "Test"},
        )

        assert response.status_code == 422

    def test_nonexistent_template(self, authenticated_client) -> None:
        """Verifieer foutmelding bij onbekend template."""
        response = authenticated_client.post(
            "/api/generate/template",
            json={"template": "does_not_exist_xyz"},
        )

        assert response.status_code in (404, 500)
