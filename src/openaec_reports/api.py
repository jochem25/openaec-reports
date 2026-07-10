"""HTTP API — FastAPI server voor PDF rapport generatie."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from openaec_reports import __version__
from openaec_reports.admin.routes import admin_router
from openaec_reports.auth.api_keys import ApiKeyDB
from openaec_reports.auth.dependencies import (
    get_current_user,
    init_api_key_db,
    init_organisation_db,
    init_user_db,
)
from openaec_reports.auth.models import OrganisationDB, User, UserDB
from openaec_reports.auth.routes import auth_router
from openaec_reports.auth.security import enforce_jwt_secret
from openaec_reports.core.cors_middleware import TenantAwareCORSMiddleware
from openaec_reports.core.data_transform import transform_json_to_engine_data
from openaec_reports.core.engine import Report
from openaec_reports.core.renderer_v2 import ReportGeneratorV2
from openaec_reports.core.template_engine import TemplateEngine
from openaec_reports.core.tenant import TenantConfig, detect_tenants_root
from openaec_reports.core.tenant_cors import (
    build_allowed_origins_set,
    load_tenant_cors_configs,
)
from openaec_reports.core.tenant_resolver import (
    get_brand_loader,
    get_template_loader,
    get_tenant_config,
)
from openaec_reports.storage.models import ReportDB
from openaec_reports.storage.routes import (
    init_report_db,
    project_router,
    report_router,
)

logger = logging.getLogger(__name__)

# Default brand naam als er geen brand in de request data zit.
# Env-driven zodat deployments hun eigen canonical tenant kunnen kiezen
# zonder code te patchen.
_DEFAULT_BRAND = os.environ.get("OPENAEC_DEFAULT_BRAND", "default")


def _find_schema_path() -> Path | None:
    """Zoek report.schema.json op meerdere locaties."""
    candidates = [
        # In package (na pip install via force-include)
        Path(__file__).parent / "schemas" / "report.schema.json",
        # In source tree (development)
        Path(__file__).parent.parent.parent / "schemas" / "report.schema.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


SCHEMA_PATH = _find_schema_path()

# Tenant configuratie — leest OPENAEC_TENANT_DIR environment variable
tenant_config = TenantConfig()

# Stationery fallback: tenant-config → OPENAEC_STATIONERY_DIR → package default.
# Geen hardcoded klant meer; gebruik een neutrale placeholder onder
# ``assets/stationery/default`` (of de actieve tenant). Eindconsumenten
# kunnen via OPENAEC_STATIONERY_DIR expliciet overschrijven.
_default_stationery = str(
    Path(__file__).parent / "assets" / "stationery" / _DEFAULT_BRAND
)
STATIONERY_DIR = tenant_config.stationery_dir or Path(
    os.environ.get("OPENAEC_STATIONERY_DIR", _default_stationery)
)
_default_uploads = str(Path(__file__).parent.parent.parent / "uploads")
UPLOAD_DIR = Path(os.environ.get("OPENAEC_UPLOAD_DIR", _default_uploads))
ASSETS_DIR = Path(__file__).parent / "assets"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle voor de FastAPI app.

    Startup:
    - JWT secret enforcement (RuntimeError in productie bij default secret)
    - Brand session cleanup (verwijder sessies ouder dan 24 uur)
    """
    # --- Startup ---
    enforce_jwt_secret()

    from openaec_reports.brand_api import cleanup_stale_sessions
    cleanup_stale_sessions()

    yield
    # --- Shutdown ---


app = FastAPI(
    title="OpenAEC Report Generator API",
    description="HTTP API voor het genereren van professionele engineering rapporten.",
    version=__version__,
    lifespan=lifespan,
)

# ============================================================
# CORS — tenant-aware (Golf 5c B-4, 2026-04-21)
# ============================================================
# Origins worden dynamisch geladen uit ``tenants/<slug>/tenant.yaml`` onder
# ``OPENAEC_TENANTS_ROOT``. Bij ontbrekende tenants-dir of lege union valt
# de middleware terug op de ``CORS_ORIGINS`` env var zodat bestaande
# productie-deploys tijdens de transitie blijven werken.

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "https://report.open-aec.com",
]


def _resolve_allowed_origins() -> tuple[frozenset[str], str]:
    """Bouw de toegestane-origin set op basis van tenant.yaml files.

    Returns:
        (origins, source) — ``source`` is een korte string voor startup-log
        zodat we kunnen zien of we op tenants of de env-fallback draaien.
    """
    tenants_root = detect_tenants_root()
    environment = os.environ.get("OPENAEC_ENV", "development")
    include_dev = environment != "production"

    if tenants_root is not None:
        configs = load_tenant_cors_configs(tenants_root, include_dev=include_dev)
        allowed = build_allowed_origins_set(configs)
        if allowed:
            logger.info(
                "CORS: %d origin(s) geladen uit %d tenant(s) onder %s (include_dev=%s)",
                len(allowed),
                len(configs),
                tenants_root,
                include_dev,
            )
            return allowed, f"tenants:{tenants_root}"
        logger.warning(
            "CORS: tenants_root=%s gevonden maar geen origins opgeleverd — fallback op env",
            tenants_root,
        )
    else:
        logger.warning(
            "CORS: geen OPENAEC_TENANTS_ROOT / OPENAEC_TENANT_DIR gezet — fallback op env"
        )

    _cors_env = os.environ.get("CORS_ORIGINS", "")
    fallback_list = (
        [o.strip() for o in _cors_env.split(",") if o.strip()]
        if _cors_env
        else _DEFAULT_CORS_ORIGINS
    )
    logger.info(
        "CORS: fallback — %d origin(s) uit %s",
        len(fallback_list),
        "CORS_ORIGINS env" if _cors_env else "hardcoded defaults",
    )
    return frozenset(fallback_list), "env"


_cors_allowed_origins, _cors_source = _resolve_allowed_origins()

app.add_middleware(
    TenantAwareCORSMiddleware,
    allowed_origins=_cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Auth setup
# ============================================================

_user_db = UserDB()
init_user_db(_user_db)

_api_key_db = ApiKeyDB()
init_api_key_db(_api_key_db)

_organisation_db = OrganisationDB()
init_organisation_db(_organisation_db)

# Report/project storage (zelfde database als auth)
_report_db = ReportDB(db_path=_user_db.db_path)
init_report_db(_report_db)

# Auth routes (login/logout/register zijn zelf open, /me checkt intern)
app.include_router(auth_router)

# Admin routes (require_admin dependency op de router zelf)
app.include_router(admin_router)

# Project en rapport routes (authenticatie op de router)
app.include_router(project_router)
app.include_router(report_router)

# Cloud storage routes (Nextcloud WebDAV) — alleen als geconfigureerd
from openaec_reports.cloud import cloud_router, is_cloud_configured

if is_cloud_configured():
    app.include_router(cloud_router)
    logger.info("Nextcloud cloud storage enabled")
else:
    logger.info("Nextcloud cloud storage disabled (env vars not set)")

# Protected router — alle business endpoints vereisen authenticatie
_protected = APIRouter(dependencies=[Depends(get_current_user)])


# ============================================================
# Helpers
# ============================================================


def _resolve_brand_with_tenant_check(data: dict, user: User) -> str:
    """Resolve brand met tenant-isolatie check.

    Als de user een tenant heeft, mag alleen de eigen brand of een
    template-afgeleide brand die matcht worden gebruikt. Andermans
    brand is niet toegestaan.

    Args:
        data: Request data dict.
        user: De geauthenticeerde user.

    Returns:
        Gevalideerde brand naam.

    Raises:
        HTTPException: Als de brand niet bij de tenant hoort.
    """
    requested_brand = data.get("brand")
    template_brand = _resolve_brand_from_template(data, user.tenant)
    tenant = user.tenant

    # Geen tenant → geen restrictie (backward compat / admin)
    if not tenant:
        return requested_brand or template_brand or _DEFAULT_BRAND

    # Expliciete brand in request → moet matchen met eigen tenant
    if requested_brand and requested_brand != tenant:
        raise HTTPException(
            status_code=403,
            detail="Brand niet toegestaan voor deze tenant",
        )

    return requested_brand or template_brand or tenant or _DEFAULT_BRAND


def _resolve_brand_from_template(data: dict, tenant_slug: str = "") -> str | None:
    """Leid brand af uit het template's tenant veld.

    Als de request geen ``brand`` bevat maar wel een ``template``, kijk dan
    of het template een ``tenant`` veld heeft en gebruik dat als brand.
    """
    template_name = data.get("template")
    if not template_name:
        return None
    try:
        loader = get_template_loader(tenant_slug)
        config = loader.load(template_name)
        return config.tenant if config.tenant else None
    except (FileNotFoundError, Exception):
        return None


def _generate_and_respond(
    build_fn: callable,
    data: dict,
) -> FileResponse:
    """Genereer PDF en retourneer als FileResponse met cleanup.

    Args:
        build_fn: Callable die een output_path ontvangt en de PDF genereert.
        data: Report data dict (voor bestandsnaam).

    Returns:
        FileResponse met de gegenereerde PDF.
    """
    output_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            output_path = Path(tmp.name)

        build_fn(output_path)

        filename = _safe_filename(
            data.get("project_number", ""),
            data.get("project", ""),
        )

        return FileResponse(
            path=str(output_path),
            media_type="application/pdf",
            filename=filename,
            background=BackgroundTask(lambda: output_path.unlink(missing_ok=True)),
        )
    except HTTPException:
        if output_path and output_path.exists():
            output_path.unlink(missing_ok=True)
        raise
    except Exception:
        if output_path and output_path.exists():
            output_path.unlink(missing_ok=True)
        raise


def _safe_filename(*parts: str, extension: str = ".pdf") -> str:
    """Maak een veilige bestandsnaam van project info.

    Args:
        *parts: Onderdelen van de bestandsnaam (project_number, project, etc.).
        extension: Bestandsextensie.

    Returns:
        Gesanitizede bestandsnaam.
    """
    combined = "_".join(p for p in parts if p)
    safe = re.sub(r"[^\w\s-]", "", combined).strip()
    safe = re.sub(r"[-\s]+", "_", safe)
    return (safe or "rapport") + extension


# ============================================================
# Exception handlers
# ============================================================


@app.exception_handler(FileNotFoundError)
async def file_not_found_handler(request: Request, exc: FileNotFoundError):
    """Ontbrekende template of brand → 404."""
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc), "type": "FileNotFoundError"},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Ongeldige data → 422."""
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "type": "ValueError"},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Onverwachte fout → 500 met type info."""
    logger.exception("Onverwachte fout bij %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Interne serverfout", "type": type(exc).__name__},
    )


# ============================================================
# Endpoints
# ============================================================


@app.get("/api/health")
async def health():
    """Health check endpoint.

    Returns:
        Status, versie en build-marker. ``build`` bevat de git-commit die in
        het image is gebakken (``OPENAEC_BUILD`` env, gezet bij ``docker build
        --build-arg GIT_COMMIT=...``). Zo kan een consument objectief
        verifiëren welke code draait i.p.v. op gedrag te moeten afgaan.
    """
    return {
        "status": "ok",
        "version": __version__,
        "build": os.environ.get("OPENAEC_BUILD", "unknown"),
    }


@_protected.get("/api/templates")
async def list_templates(user: User = Depends(get_current_user)):
    """Lijst beschikbare rapport templates.

    Returns:
        Dict met lijst van templates (naam + type).
    """
    loader = get_template_loader(user.tenant)
    return {"templates": loader.list_templates()}


@_protected.get("/api/templates/{name}/scaffold")
async def get_template_scaffold(name: str, user: User = Depends(get_current_user)):
    """Retourneer een leeg JSON scaffold voor een template.

    De frontend kan dit laden als startpunt voor een nieuw rapport.

    Args:
        name: Template naam.

    Returns:
        Dict conform report.schema.json met defaults uit het template.
    """
    loader = get_template_loader(user.tenant)
    scaffold = loader.to_scaffold(name)
    return scaffold


@_protected.get("/api/brands")
async def list_brands(user: User = Depends(get_current_user)):
    """Lijst beschikbare brand configuraties.

    Returns:
        Dict met lijst van brands (naam + slug).
    """
    loader = get_brand_loader(user.tenant)
    return {"brands": loader.list_brands()}


@_protected.post("/api/validate")
async def validate_report(request: Request):
    """Valideer JSON data tegen report.schema.json.

    Body:
        JSON data conform report.schema.json.

    Returns:
        {"valid": true} of {"valid": false, "errors": [...]}.
    """
    import jsonschema

    data = await request.json()

    if SCHEMA_PATH is None:
        raise HTTPException(
            status_code=500,
            detail="Schema bestand niet gevonden — validatie niet beschikbaar",
        )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = [
        {
            "path": "/".join(str(p) for p in e.absolute_path),
            "message": e.message,
        }
        for e in validator.iter_errors(data)
    ]
    return {"valid": len(errors) == 0, "errors": errors}


@_protected.post("/api/generate")
async def generate_report(request: Request, user: User = Depends(get_current_user)):
    """Genereer PDF rapport uit JSON data.

    Body:
        JSON data conform report.schema.json.

    Returns:
        PDF bestand als binary response (application/pdf).
    """
    data = await request.json()

    if not data.get("project"):
        raise HTTPException(status_code=422, detail="Veld 'project' is verplicht")
    if not data.get("template"):
        raise HTTPException(status_code=422, detail="Veld 'template' is verplicht")

    # Inject user profiel defaults in colofon
    _inject_user_profile_defaults(data, user)

    brand = _resolve_brand_with_tenant_check(data, user)
    report = Report.from_dict(data, brand=brand)

    def build(output_path: Path) -> None:
        report.build(output_path)

    return _generate_and_respond(build, data)


# ============================================================
# V2 Endpoints — ReportGeneratorV2
# ============================================================


def _inject_user_profile_defaults(data: dict, user: User) -> None:
    """Vul colofon adviseur-velden in vanuit het user profiel.

    Alleen lege velden worden ingevuld (setdefault patroon).
    Werkt voor alle auth-methoden (lokaal, OIDC, API key).
    Als de user een organisation_id heeft en geen company, wordt de
    organisatienaam opgehaald en gebruikt als adviseur_bedrijf.

    Args:
        data: Rapport JSON data (wordt in-place gewijzigd).
        user: De geauthenticeerde user.
    """
    colofon = data.setdefault("colofon", {})
    if user.display_name and not colofon.get("adviseur_naam"):
        colofon.setdefault("adviseur_naam", user.display_name)
    if user.email and not colofon.get("adviseur_email"):
        colofon.setdefault("adviseur_email", user.email)
    if user.phone and not colofon.get("adviseur_telefoon"):
        colofon.setdefault("adviseur_telefoon", user.phone)
    if user.job_title and not colofon.get("adviseur_functie"):
        colofon.setdefault("adviseur_functie", user.job_title)
    if user.registration_number and not colofon.get("adviseur_registratie"):
        colofon.setdefault("adviseur_registratie", user.registration_number)
    if user.company and not colofon.get("adviseur_bedrijf"):
        colofon.setdefault("adviseur_bedrijf", user.company)
    # Fallback: haal organisatienaam op als company leeg maar organisation_id gezet is
    if not colofon.get("adviseur_bedrijf") and user.organisation_id:
        try:
            from openaec_reports.auth.dependencies import get_organisation_db
            org_db = get_organisation_db()
            org = org_db.get_by_id(user.organisation_id)
            if org and org.name:
                colofon.setdefault("adviseur_bedrijf", org.name)
        except RuntimeError:
            pass


@_protected.post("/api/generate/v2")
async def generate_report_v2(request: Request, user: User = Depends(get_current_user)):
    """Genereer PDF rapport via renderer_v2 (pixel-perfect huisstijl).

    Body:
        JSON data met project info, sections, appendices.

    Returns:
        PDF bestand als binary response.
    """
    data = await request.json()

    if not data.get("project"):
        raise HTTPException(status_code=422, detail="Veld 'project' is verplicht")

    # Inject user profiel defaults in colofon
    _inject_user_profile_defaults(data, user)

    brand = _resolve_brand_with_tenant_check(data, user)

    tc = get_tenant_config(user.tenant)
    stationery_dir = tc.stationery_dir or STATIONERY_DIR

    # Resolve stationery: brand_dir/stationery → tenant → package
    brand_loader = get_brand_loader(user.tenant)
    brand_config = brand_loader.load(brand)
    if brand_config.brand_dir:
        brand_stat = brand_config.brand_dir / "stationery"
        if brand_stat.exists():
            stationery_dir = brand_stat
    elif tc.stationery_dir and tc.stationery_dir.exists():
        stationery_dir = tc.stationery_dir
    else:
        brand_stationery = ASSETS_DIR / "stationery" / brand
        if brand_stationery.exists():
            stationery_dir = brand_stationery

    # Tenant_slug doorgeven zodat TemplateSet/FontManager de cascade gebruiken
    # (tenant bind-mount → package defaults). Zonder slug valt alles terug op
    # de oude package-only resolver.
    generator = ReportGeneratorV2(brand=brand, tenant_slug=user.tenant or None)

    def build(output_path: Path) -> None:
        generator.generate(data, stationery_dir, output_path)

    return _generate_and_respond(build, data)


# ============================================================
# Template Engine Endpoint — YAML-driven multi-tenant
# ============================================================


def _resolve_tenant_and_template(
    template_name: str,
    tenants_dir: Path,
    data: dict,
    user: User,
) -> tuple[str, str]:
    """Leid tenant en template naam af uit de template identifier.

    Tenant-isolatie: de user mag ALLEEN templates van eigen tenant gebruiken.
    Als het template een tenant-prefix heeft die matcht met user.tenant,
    wordt de prefix gestript. Andere tenant-prefixes worden geweigerd.

    Args:
        template_name: Template naam (mogelijk met tenant prefix).
        tenants_dir: Root directory met alle tenant subdirectories.
        data: Request data dict.
        user: De geauthenticeerde user.

    Returns:
        Tuple van (tenant, template_name).
    """
    tenant = user.tenant or data.get("brand") or ""

    if not tenant:
        return "", template_name

    # Strip eigen tenant prefix als aanwezig
    prefix = tenant + "_"
    if template_name.startswith(prefix):
        return tenant, template_name[len(prefix):]

    return tenant, template_name


@_protected.post("/api/generate/template")
async def generate_template_report(request: Request, user: User = Depends(get_current_user)):
    """Genereer PDF rapport via TemplateEngine (YAML-driven, multi-tenant)."""
    data = await request.json()

    # Inject user profiel defaults in colofon
    _inject_user_profile_defaults(data, user)

    template_name = data.get("template")
    if not template_name:
        raise HTTPException(status_code=422, detail="Veld 'template' is verplicht")

    # Resolve tenants directory EERST — nodig voor tenant detectie
    tenants_dir = _resolve_tenants_dir()

    if not tenants_dir or not tenants_dir.exists():
        raise HTTPException(
            status_code=500,
            detail="Tenants directory niet gevonden — configureer OPENAEC_TENANTS_ROOT",
        )

    # Resolve tenant uit template naam (niet uit user sessie!)
    tenant, template_name = _resolve_tenant_and_template(
        template_name, tenants_dir, data, user,
    )

    logger.info("TemplateEngine: tenant=%s, template=%s", tenant, template_name)

    # Brand = tenant (Customer brand.yaml zit in tenants/customer/)
    brand = _resolve_brand_with_tenant_check(data, user)

    # Transformeer JSON data naar engine formaat
    engine_data = transform_json_to_engine_data(data)

    engine = TemplateEngine(tenants_dir=tenants_dir)

    def build(output_path: Path) -> None:
        engine.build(
            template_name=template_name,
            tenant=tenant,
            data=engine_data,
            output_path=output_path,
            brand=brand,
        )

    return _generate_and_respond(build, data)


def _resolve_tenants_dir(tenant: str = "") -> Path | None:
    """Resolve de tenants ROOT directory (bevat alle tenant subdirectories).

    Probeert in volgorde:
    1. OPENAEC_TENANTS_ROOT environment variable (expliciet)
    2. Parent van OPENAEC_TENANT_DIR (als die meerdere tenant dirs bevat)
    3. Source tree: <package>/../../tenants
    4. Package-relatief: <package>/tenants
    """
    # 1. Expliciete tenants root
    root_env = os.environ.get("OPENAEC_TENANTS_ROOT")
    if root_env:
        root = Path(root_env)
        if root.exists():
            return root

    # 2. Parent van OPENAEC_TENANT_DIR
    td_env = os.environ.get("OPENAEC_TENANT_DIR")
    if td_env:
        parent = Path(td_env).parent
        # Verify: parent moet meerdere tenant dirs bevatten
        if parent.exists() and any(
            d.is_dir() and (d / "brand.yaml").exists()
            for d in parent.iterdir()
        ):
            return parent

    # 3. Source tree
    source_tenants = Path(__file__).parent.parent.parent / "tenants"
    if source_tenants.exists():
        return source_tenants

    # 4. Package-relatief
    pkg_tenants = Path(__file__).parent / "tenants"
    if pkg_tenants.exists():
        return pkg_tenants

    return None


_ALLOWED_UPLOAD_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf",
}
_MAX_UPLOAD_SIZE = 10_485_760  # 10 MB


@_protected.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload een afbeelding voor gebruik in rapporten.

    Returns:
        Dict met pad dat als `src` in JSON content gebruikt kan worden.

    Raises:
        HTTPException: Bij ongeldig bestandstype of te groot bestand.
    """
    ext = Path(file.filename or "upload.png").suffix.lower() or ".png"
    if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Bestandstype '{ext}' niet toegestaan. "
            f"Toegestaan: {', '.join(sorted(_ALLOWED_UPLOAD_EXTENSIONS))}",
        )

    # Lees content en check grootte
    content = await file.read()
    if len(content) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Bestand te groot ({len(content)} bytes). Maximum: {_MAX_UPLOAD_SIZE} bytes (10 MB)",
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / unique_name

    dest.write_bytes(content)

    return {
        "path": str(dest),
        "filename": unique_name,
        "size": len(content),
    }


@_protected.get("/api/stationery")
async def list_stationery(user: User = Depends(get_current_user)):
    """Retourneer stationery status voor de huidige tenant.

    Toont uitsluitend de stationery van de eigen tenant. Andere
    tenants' stationery is nooit zichtbaar.

    Returns:
        Dict met brands en per brand de beschikbare stationery bestanden.
    """
    required = ["colofon.pdf", "standaard.pdf", "bijlagen.pdf", "achterblad.pdf"]
    brands = {}

    def _scan_stationery_dir(sdir: Path, label: str) -> None:
        if not sdir.exists() or not sdir.is_dir():
            return
        files = {f.name: True for f in sdir.iterdir() if f.is_file()}
        if files:
            brands[label] = {
                "complete": all(r in files for r in required),
                "files": list(files.keys()),
                "missing": [r for r in required if r not in files],
            }

    # Alleen tenant stationery — geen cross-tenant scan
    tc = get_tenant_config(user.tenant)
    tenant_stat = tc.stationery_dir
    if tenant_stat and tenant_stat.exists():
        _scan_stationery_dir(
            tenant_stat, tenant_stat.name if tenant_stat.name != "stationery" else "tenant"
        )
    elif user.tenant:
        # Fallback: package stationery voor EIGEN tenant
        brand_stationery = ASSETS_DIR / "stationery" / user.tenant
        if brand_stationery.exists():
            _scan_stationery_dir(brand_stationery, user.tenant)

    return {"brands": brands}


# Protected router mounten op de app
app.include_router(_protected)

# Brand onboarding API (eigen auth dependency)
from openaec_reports.brand_api import brand_router  # noqa: E402

app.include_router(brand_router)

# ============================================================
# Documentation endpoint (voor admin Help tab)
# ============================================================


def _find_docs_dir() -> Path | None:
    """Zoek docs/ op meerdere locaties."""
    candidates = [
        Path(__file__).parent.parent.parent / "docs",  # dev
        Path("/app/docs"),                              # Docker
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


@app.get("/api/docs/architecture")
async def serve_architecture_docs():
    """Serve de architecture HTML documentatie voor het admin panel."""
    docs_dir = _find_docs_dir()
    arch_dir = docs_dir / "architecture" if docs_dir else None
    if not arch_dir or not (arch_dir / "architecture.html").exists():
        raise HTTPException(status_code=404, detail="Documentation not found")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=(arch_dir / "architecture.html").read_text(encoding="utf-8"))


@app.get("/api/uitleg")
async def serve_uitleg():
    """Serve de publieke handleiding pagina."""
    docs_dir = _find_docs_dir()
    uitleg_path = docs_dir / "uitleg.html" if docs_dir else None
    if not uitleg_path or not uitleg_path.exists():
        raise HTTPException(status_code=404, detail="Handleiding niet gevonden")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=uitleg_path.read_text(encoding="utf-8"))

# ============================================================
# PDOK kaarten
# ============================================================


@_protected.get("/api/pdok/map")
async def pdok_map(
    lat: float,
    lon: float,
    radius: float = 500.0,
    service: str = "luchtfoto",
    layers: str | None = None,
    width: int = 1600,
    height: int = 1200,
    user: User = Depends(get_current_user),
):
    """Haal een kaartafbeelding op via PDOK WMS.

    Args:
        lat: Breedtegraad (WGS84).
        lon: Lengtegraad (WGS84).
        radius: Straal rondom punt in meters (default 500).
        service: PDOK service: luchtfoto, kadaster, bgt, bag.
        layers: Laagnamen (komma-gescheiden). Default per service.
        width: Breedte in pixels (max 4000).
        height: Hoogte in pixels (max 4000).

    Returns:
        Kaartafbeelding als JPEG of PNG.
    """
    from fastapi.responses import Response

    # Validatie
    width = min(width, 4000)
    height = min(height, 4000)
    radius = min(max(radius, 10), 10000)

    # Default layers per service
    default_layers = {
        "luchtfoto": "Actueel_orthoHR",
        "kadaster": "Kadastralekaart",
        "bag": "pand",
    }
    if not layers:
        layers = default_layers.get(service, "Actueel_orthoHR")

    # Formaat: luchtfoto als JPEG (kleiner), rest als PNG
    img_format = "image/jpeg" if service == "luchtfoto" else "image/png"

    try:
        from openaec_reports.data.kadaster import KadasterClient

        client = KadasterClient()
        x, y = client.wgs84_to_rd(lat, lon)
        bbox = f"{x - radius},{y - radius},{x + radius},{y + radius}"

        import requests as req

        # Kadaster v5 vereist uppercase params
        url = client.WMS_SERVICES.get(service, client.WMS_SERVICES["luchtfoto"])
        params = {
            "SERVICE": "WMS",
            "VERSION": "1.3.0",
            "REQUEST": "GetMap",
            "LAYERS": layers,
            "CRS": "EPSG:28992",
            "BBOX": bbox,
            "WIDTH": width,
            "HEIGHT": height,
            "FORMAT": img_format,
            "STYLES": "",
        }
        resp = req.get(url, params=params, timeout=30)
        resp.raise_for_status()

        # Check of het een echte afbeelding is (niet XML error)
        ct = resp.headers.get("content-type", "")
        if "xml" in ct:
            raise HTTPException(
                status_code=502,
                detail="PDOK service retourneerde een fout",
            )

        media_type = "image/jpeg" if img_format == "image/jpeg" else "image/png"
        return Response(content=resp.content, media_type=media_type)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PDOK fout: {exc}") from exc


@_protected.get("/api/pdok/services")
async def pdok_services(user: User = Depends(get_current_user)):
    """Lijst beschikbare PDOK kaartservices.

    Returns:
        Lijst van services met naam, beschrijving en default layers.
    """
    return {
        "services": [
            {
                "id": "luchtfoto",
                "label": "Luchtfoto",
                "description": "Actuele luchtfoto (PDOK)",
                "default_layers": "Actueel_orthoHR",
                "format": "image/jpeg",
            },
            {
                "id": "kadaster",
                "label": "Kadastrale kaart",
                "description": "Perceelgrenzen en nummers",
                "default_layers": "Kadastralekaart",
                "format": "image/png",
            },
            {
                "id": "bag",
                "label": "BAG Bebouwing",
                "description": "Panden uit de BAG",
                "default_layers": "pand",
                "format": "image/png",
            },
        ]
    }


# ============================================================
# Static frontend (moet ONDERAAN staan, na alle API routes)
# ============================================================

_static_dir = Path(__file__).parent.parent.parent / "static"
if _static_dir.exists():
    # Kopieer uitleg.html naar static dir zodat /uitleg.html direct werkt
    _docs_dir = _find_docs_dir()
    if _docs_dir and (_docs_dir / "uitleg.html").exists():
        shutil.copy2(_docs_dir / "uitleg.html", _static_dir / "uitleg.html")
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


# ============================================================
# Entrypoint
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("openaec_reports.api:app", host="0.0.0.0", port=8000, reload=True)
