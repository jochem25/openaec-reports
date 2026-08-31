# OpenAEC Reports

Multi-tenant PDF rapportgenerator. Python backend (ReportLab + PyMuPDF) met parallelle Rust implementatie (Typst + lopdf) in monorepo. REST API, frontend editor, Tauri desktop app.

Live op [`report.open-aec.com`](https://report.open-aec.com).

---

## Doel

PDF-rapporten genereren vanuit JSON-data met pixel-perfecte layout, klant-specifieke huisstijlen (per-tenant brand + templates + stationery), en zowel flow-based content-blocks (V1) als fixed-page templates (V2/V3).

Engines parallel live op productie:

| Engine | Endpoint | Gebruik |
|---|---|---|
| V1 `Report.from_dict()` | `/api/generate` | Flow-based content-blocks (legacy) |
| V2 `ReportGeneratorV2` | `/api/generate/v2` | Pixel-perfect via `renderer_v2` |
| V3 `TemplateEngine` | `/api/generate/template` | YAML page_types, fixed-page layouts |
| V3-Rust `openaec_engine::Engine` | (zelfde endpoint, alternatief) | Pure Rust V3, wacht op `report-rs.open-aec.com` |

---

## Architectuur

Monorepo met drie taalgebieden:

- **Python backend** (`src/openaec_reports/`): FastAPI app, 3 engines, auth (OIDC + API keys), multi-tenant, admin panel routes
- **Rust crates** (`rust/crates/`): `openaec-layout` (eigen ReportLab-port), `openaec-engine` (V3 in pure Rust), Axum server
- **Frontend** (`frontend/`): React + TypeScript + Vite editor met Backstage / Ribbon / ProjectBrowser, OIDC (PKCE)
- **Desktop** (`src-tauri/`): Tauri v2 thin wrapper, v0.2.0-alpha draft

---

## Belangrijke bestanden

| Pad | Doel |
|---|---|
| `src/openaec_reports/api.py` | FastAPI app, 3 generate endpoints |
| `src/openaec_reports/core/engine.py` | V3 TemplateEngine (Python) |
| `src/openaec_reports/core/renderer_v2.py` | V2 pixel-perfect renderer |
| `src/openaec_reports/auth/` | OIDC + API keys + session JWT |
| `src/openaec_reports/storage/` | SQLite models (projects, reports, users, api_keys) |
| `src/openaec_reports/admin/routes.py` | Admin panel CRUD (tenants, templates, brands) |
| `rust/crates/openaec-engine/` | V3 TemplateEngine in pure Rust (~2400 LOC, 28 tests) |
| `rust/crates/openaec-layout/` | Rust port van ReportLab-primitives |
| `schemas/report.schema.json` | Primaire JSON schema |
| `schemas/bic_rapport.schema.json` | BIC-specifiek schema (17 pagina's) |
| `tenants/default/` | Public placeholder tenant (OFL fonts) |
| `deploy.sh` | Git pull → Docker build → deploy → health check |

Private tenant-directories (brand, templates, stationery, fonts) zitten **niet in git** — via host bind-mount vanuit `/opt/openaec/reports-tenants/` op productie.

---

## Bootsequence (bij elke Claude Code sessie)

Lees in volgorde:

1. `STATUS.md` — huidige staat, actieve sporen, engines-tabel, deployment
2. `TODO.md` — open items (🔴 Kritiek → 🟡 Middel → 🟢 Laag)
3. `archief/2026-Q1-voltooid.md` — alleen raadplegen als historisch spoor nodig is
4. `frontend/CLAUDE.md` — frontend-specifieke conventies (als je in frontend werkt)
5. Orchestrator: `C:\Users\JoKo\.claude\orchestrator\sessions\openaec-reports_latest.md` — laatste handoff van PM-laag

---

## Conventies

- **Python:** PEP 8 via ruff (config: `line-length = 100`), pytest voor tests (1264 collected, 1214 passed, 50 skipped)
- **Rust:** `cargo test` groen op alle crates
- **TypeScript:** strict mode, Biome formatter, geen `as any` casts (open CR-M8)
- **Multi-tenant isolatie:** elke `/api/*` route moet `user.tenant` vs resource-tenant valideren (zie SEC-K1, SEC-K2, SEC-H1, SEC-H2)
- **Admin endpoints:** tenant-check verplicht via `if user.tenant and user.tenant != tenant: raise 403`
- **JSON schemas:** Python en Rust delen schema als contract; Rust is drop-in vervanging voor Python
- **OIDC:** Authentik als IdP, `sub_mode=hashed_user_id` voor unieke user-identificatie, tenant wordt **admin-managed** (niet uit OIDC claims)
- **API keys:** prefix `oaec_k_`, alleen plaintext bij creatie, revocable via admin panel

---

## Informatie-routing (conform orchestrator CLAUDE.md §Informatie-routing)

**Bugs, features, tech-debt met `bestand:regel` ref** → `TODO.md` in deze repo, geen cross-write naar orchestrator
**Cross-project coördinatie / platform-blokkades** → `D:\dev\openaec\openaec-orchestrator\TODO-*.md`
**Sessie-handoff ("vandaag gedaan, morgen doen")** → `C:\Users\JoKo\.claude\orchestrator\sessions\openaec-reports_latest.md`, **max 8 KB**

**Grootte-drempels:**
- `TODO.md` > 10 KB → archiveer historie naar `archief/YYYY-Qn-voltooid.md`
- `STATUS.md` > 10 KB → zelfde
- Code-diffs met regelnummers horen in git log, niet in markdown files

---

## Deployment

- **Productie:** `report.open-aec.com`, Hetzner VPS via Docker, multi-stage Dockerfile (Node frontend + Python backend)
- **CI/CD:** GitHub Actions (`.github/workflows/ci.yml`), `deploy.sh` script
- **Multi-tenant:** host bind-mount `/opt/openaec/reports-tenants/` → container `/app/tenants`
- **Health check:** `GET /api/health` → `{"status":"ok"}`
- **Secrets:** `OPENAEC_JWT_SECRET`, `OPENAEC_DEFAULT_BRAND`, `OPENAEC_REGISTRATION_ENABLED`, Nextcloud credentials via `_get_nextcloud_*()` lazy loaders

---

## Agent Broker
- **project_id:** `openaec-reports`
- **display_name:** `OpenAEC Reports`
- **capabilities:** `["pdf", "reportlab", "pymupdf", "rust", "typst", "multi-tenant"]`
- **subscriptions:** `["shared/*", "warmteverlies/*", "pyrevit/*"]`

---

## Sessie-einde

Schrijf handoff naar `C:\Users\JoKo\.claude\orchestrator\sessions\openaec-reports_latest.md` (max 8 KB). Gebruik dit formaat:

```markdown
# OpenAEC Reports — Sessie update
**Datum:** YYYY-MM-DD HH:MM
**Branch:** (git branch)

## Wat is gedaan
- (bullets)

## Huidige staat
(1-3 zinnen)

## Openstaand / next steps
- (bullets)

## Cross-project notities
(relevantie voor warmteverlies / pyrevit / orchestrator)
```

**Orchestrator context-file:** `C:\Users\JoKo\.claude\orchestrator\context\report.md`
**Project registry:** `C:\Users\JoKo\.claude\orchestrator\project-registry.json` (entry `openaec-reports`)
