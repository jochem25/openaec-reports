"""Nextcloud WebDAV integration for cloud storage of generated reports.

Provides REST endpoints to list Nextcloud project directories and save
generated PDF reports directly to the project's cloud folder.

Supports both the new project container model (reports/) and the legacy
layout (99_overige_documenten/reports/) with automatic fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from openaec_reports.auth.dependencies import get_current_user
from openaec_reports.auth.models import User

logger = logging.getLogger(__name__)

# --- Project container model constants ---
DIR_REPORTS = "reports"
MANIFEST_FILENAME = "project.wefc"

# Legacy path components (backward compatibility)
LEGACY_SUBDIR = "99_overige_documenten"
TOOL_SUBDIR = "reports"

# Application identifier for manifest header
APPLICATION_NAME = "openaec-reports"

# --- Lazy-loaded Nextcloud credentials ---
# Credentials worden pas bij eerste gebruik opgehaald, niet bij module import.
# Dit voorkomt dat secrets als module-level constanten in geheugen staan.


def _get_nextcloud_url() -> str:
    """Lees Nextcloud URL lazy uit environment."""
    return os.getenv("NEXTCLOUD_URL", "")


def _get_nextcloud_user() -> str:
    """Lees Nextcloud service user lazy uit environment."""
    return os.getenv("NEXTCLOUD_SERVICE_USER", "")


def _get_nextcloud_pass() -> str:
    """Lees Nextcloud service password lazy uit environment."""
    return os.getenv("NEXTCLOUD_SERVICE_PASS", "")

DAV_NAMESPACE = {"d": "DAV:"}


def is_cloud_configured() -> bool:
    """Check if Nextcloud environment variables are set."""
    return bool(_get_nextcloud_url() and _get_nextcloud_user() and _get_nextcloud_pass())


def _dav_base() -> str:
    """WebDAV base URL for the service account's Projects folder."""
    base = _get_nextcloud_url().rstrip("/")
    return f"{base}/remote.php/dav/files/{_get_nextcloud_user()}/Projects"


def _new_path(project: str) -> str:
    """WebDAV URL for the new project container reports directory."""
    return f"{_dav_base()}/{quote(project)}/{DIR_REPORTS}"


def _legacy_path(project: str) -> str:
    """WebDAV URL for the legacy reports directory (backward compat)."""
    return f"{_dav_base()}/{quote(project)}/{LEGACY_SUBDIR}/{TOOL_SUBDIR}"


def _tool_path(project: str) -> str:
    """Full WebDAV URL for a project's tool subdirectory.

    Uses the new path (reports/) as default.
    """
    return _new_path(project)


def _file_url(project: str, filename: str) -> str:
    """Full WebDAV URL for a specific file in the new reports directory."""
    return f"{_new_path(project)}/{quote(filename)}"


def _manifest_url(project: str) -> str:
    """WebDAV URL for the project manifest (project.wefc)."""
    return f"{_dav_base()}/{quote(project)}/{MANIFEST_FILENAME}"


def _get_client() -> httpx.Client:
    """Create an httpx client with Nextcloud Basic auth."""
    return httpx.Client(
        auth=(_get_nextcloud_user(), _get_nextcloud_pass()),
        timeout=30.0,
    )


def _parse_collections(xml_text: str) -> list[str]:
    """Parse PROPFIND XML to extract collection (directory) names."""
    names: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return names

    for response in root.findall("d:response", DAV_NAMESPACE):
        href_el = response.find("d:href", DAV_NAMESPACE)
        if href_el is None or href_el.text is None:
            continue

        # Check if it's a collection
        restype = response.find(".//d:resourcetype/d:collection", DAV_NAMESPACE)
        if restype is None:
            continue

        href = unquote(href_el.text.rstrip("/"))
        # Skip the base directory itself (ends with /Projects)
        if href.endswith("/Projects"):
            continue

        name = href.rsplit("/", 1)[-1]
        if name:
            names.append(name)

    return names


def _parse_files(xml_text: str, extension: str = ".pdf") -> list[dict[str, Any]]:
    """Parse PROPFIND XML to extract file entries."""
    files: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return files

    for response in root.findall("d:response", DAV_NAMESPACE):
        href_el = response.find("d:href", DAV_NAMESPACE)
        if href_el is None or href_el.text is None:
            continue

        # Skip collections
        restype = response.find(".//d:resourcetype/d:collection", DAV_NAMESPACE)
        if restype is not None:
            continue

        name = unquote(href_el.text.rstrip("/")).rsplit("/", 1)[-1]
        if not name.endswith(extension):
            continue

        size_el = response.find(".//d:getcontentlength", DAV_NAMESPACE)
        modified_el = response.find(".//d:getlastmodified", DAV_NAMESPACE)

        files.append({
            "name": name,
            "size": int(size_el.text) if size_el is not None and size_el.text else 0,
            "last_modified": modified_el.text if modified_el is not None else "",
        })

    return files


def _ensure_directory(client: httpx.Client, url: str) -> None:
    """Ensure a WebDAV directory exists, creating it if needed."""
    res = client.request("PROPFIND", url, headers={"Depth": "0"})
    if res.status_code < 400:
        return

    res = client.request("MKCOL", url)
    if res.status_code not in (201, 405):  # 405 = already exists
        logger.warning("MKCOL %s failed: %s", url, res.status_code)


def _slugify(text: str) -> str:
    """Convert text to a safe filename slug."""
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s]+", "_", text)


def _read_manifest(client: httpx.Client, project: str) -> dict[str, Any] | None:
    """Read the project manifest (project.wefc) via WebDAV.

    Returns the parsed JSON dict, or None if no manifest exists.
    """
    url = _manifest_url(project)
    res = client.get(url)
    if res.status_code == 404:
        return None
    if res.status_code >= 400:
        logger.warning("Failed to read manifest for %s: %s", project, res.status_code)
        return None
    try:
        return res.json()
    except (json.JSONDecodeError, ValueError):
        logger.warning("Invalid JSON in manifest for project %s", project)
        return None


def _write_manifest(
    client: httpx.Client,
    project: str,
    manifest: dict[str, Any],
) -> bool:
    """Write the project manifest (project.wefc) via WebDAV.

    Returns True on success, False on failure.
    """
    url = _manifest_url(project)
    data = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    res = client.put(url, content=data, headers={"Content-Type": "application/json"})
    if res.status_code >= 400:
        logger.warning("Failed to write manifest for %s: %s", project, res.status_code)
        return False
    return True


def _create_empty_manifest() -> dict[str, Any]:
    """Create a new empty project manifest."""
    return {
        "header": {
            "schema": "WeFC",
            "schemaVersion": "1.0.0",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "application": APPLICATION_NAME,
        },
        "data": [],
    }


def _build_wefc_report(
    name: str,
    filename: str,
) -> dict[str, Any]:
    """Build a WefcReport manifest object for a generated report.

    Args:
        name: Human-readable report title.
        filename: PDF filename (e.g. "rapport_2026-03-30.pdf").

    Returns:
        WefcReport dict ready for insertion into manifest.data.
    """
    now = datetime.now(timezone.utc).isoformat()
    return {
        "type": "WefcReport",
        "guid": str(uuid.uuid4()),
        "name": name,
        "path": f"{DIR_REPORTS}/{filename}",
        "status": "active",
        "created": now,
        "modified": now,
    }


def _upsert_manifest_report(
    client: httpx.Client,
    project: str,
    report_name: str,
    filename: str,
) -> None:
    """Read-merge-write a WefcReport entry into the project manifest.

    Creates the manifest if it doesn't exist yet.
    """
    manifest = _read_manifest(client, project)
    if manifest is None:
        manifest = _create_empty_manifest()

    # Update header timestamp and application
    manifest.setdefault("header", {})
    manifest["header"]["timestamp"] = datetime.now(timezone.utc).isoformat()
    manifest["header"]["application"] = APPLICATION_NAME

    # Build report object
    report_obj = _build_wefc_report(report_name, filename)

    # Check if a report with the same path already exists — update it
    data = manifest.get("data", [])
    report_path = report_obj["path"]
    updated = False
    for i, item in enumerate(data):
        if (
            isinstance(item, dict)
            and item.get("type") == "WefcReport"
            and item.get("path") == report_path
        ):
            # Preserve original guid and created timestamp
            report_obj["guid"] = item.get("guid", report_obj["guid"])
            report_obj["created"] = item.get("created", report_obj["created"])
            data[i] = report_obj
            updated = True
            break

    if not updated:
        data.append(report_obj)

    manifest["data"] = data

    if _write_manifest(client, project, manifest):
        logger.info("Updated manifest for project %s with report %s", project, filename)
    else:
        logger.warning("Failed to update manifest for project %s", project)


# --- FastAPI Router ---

cloud_router = APIRouter(
    prefix="/api/cloud",
    tags=["cloud"],
    dependencies=[Depends(get_current_user)],
)


@cloud_router.get("/projects")
async def list_projects() -> list[dict[str, str]]:
    """List available Nextcloud project directories."""
    with _get_client() as client:
        res = client.request(
            "PROPFIND",
            f"{_dav_base()}/",
            headers={"Depth": "1"},
        )

    if res.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Nextcloud returned {res.status_code}",
        )

    projects = _parse_collections(res.text)
    return [{"name": p} for p in projects]


@cloud_router.get("/projects/{project}/files")
async def list_files(project: str) -> list[dict[str, Any]]:
    """List PDF files in a project's reports directory.

    Tries the new path (reports/) first, falls back to legacy
    (99_overige_documenten/reports/) if the new path is empty or missing.
    """
    with _get_client() as client:
        # Try new path first
        res = client.request(
            "PROPFIND",
            f"{_new_path(project)}/",
            headers={"Depth": "1"},
        )

        if res.status_code < 400:
            files = _parse_files(res.text)
            if files:
                return files

        # Fallback to legacy path
        legacy_res = client.request(
            "PROPFIND",
            f"{_legacy_path(project)}/",
            headers={"Depth": "1"},
        )

    if legacy_res.status_code == 404 and res.status_code == 404:
        return []

    if legacy_res.status_code < 400:
        legacy_files = _parse_files(legacy_res.text)
        if legacy_files:
            logger.info(
                "Project %s: using legacy path 99_overige_documenten/reports/",
                project,
            )
            return legacy_files

    # Both paths empty
    if res.status_code < 400 or legacy_res.status_code < 400:
        return []

    raise HTTPException(
        status_code=502,
        detail=f"Nextcloud returned {res.status_code}",
    )


@cloud_router.get("/projects/{project}/files/{filename}")
async def get_file(project: str, filename: str) -> Response:
    """Download a specific file from Nextcloud.

    Tries new path first, falls back to legacy path.
    """
    with _get_client() as client:
        # Try new path first
        new_url = f"{_new_path(project)}/{quote(filename)}"
        res = client.get(new_url)

        if res.status_code == 404:
            # Fallback to legacy path
            legacy_url = f"{_legacy_path(project)}/{quote(filename)}"
            res = client.get(legacy_url)

    if res.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Bestand '{filename}' niet gevonden")

    if res.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Nextcloud returned {res.status_code}",
        )

    return Response(
        content=res.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@cloud_router.post("/projects/{project}/save")
async def save_report_to_cloud(
    project: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Generate a PDF report and save it directly to Nextcloud.

    Accepts the same JSON body as /api/generate.
    Always writes to the new path (reports/), then updates project.wefc.
    """
    # Import here to avoid circular imports
    from openaec_reports.api import _resolve_brand_with_tenant_check
    from openaec_reports.core.engine import Report

    data = await request.json()

    if not data.get("project"):
        raise HTTPException(status_code=422, detail="Veld 'project' is verplicht")
    if not data.get("template"):
        raise HTTPException(status_code=422, detail="Veld 'template' is verplicht")

    brand = _resolve_brand_with_tenant_check(data, user)
    report = Report.from_dict(data, brand=brand)

    # Generate PDF to temp file
    output_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            output_path = Path(tmp.name)

        report.build(output_path)
        pdf_bytes = output_path.read_bytes()
    finally:
        if output_path and output_path.exists():
            output_path.unlink(missing_ok=True)

    # Build filename: slugified project name + title + date
    title_slug = _slugify(data.get("project", "rapport"))
    date_str = date.today().isoformat()
    filename = f"{title_slug}_{date_str}.pdf"

    # Derive report title for manifest
    report_title = data.get("title", data.get("project", "Rapport"))

    # Upload to Nextcloud — always write to new path (reports/)
    with _get_client() as client:
        reports_url = f"{_new_path(project)}/"
        _ensure_directory(client, reports_url)

        res = client.put(
            _file_url(project, filename),
            content=pdf_bytes,
            headers={"Content-Type": "application/pdf"},
        )

        if res.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Upload naar Nextcloud mislukt: {res.status_code}",
            )

        # Update project manifest (project.wefc) with WefcReport entry
        _upsert_manifest_report(client, project, report_title, filename)

    return {
        "status": "saved",
        "filename": filename,
        "project": project,
        "path": f"{DIR_REPORTS}/{filename}",
    }
