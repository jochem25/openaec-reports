# TODO — openaec-reports

**Laatst bijgewerkt:** 2026-09-28 (BVP E1-E5, E7-E9) | **Status:** [`STATUS.md`](STATUS.md) | **Archief:** [`archief/2026-Q1-voltooid.md`](archief/2026-Q1-voltooid.md)

> Legenda: 🔴 Blocker | 🟡 Middel | 🟢 Nice-to-have
> Voltooid werk is verplaatst naar `archief/2026-Q1-voltooid.md` (1 apr 2026). Deze file bevat alleen open items.

---

## 🔴 Kritiek / hoog

### Renderer brand-lekkage — Fase 1-3 afgerond, restpunten open

Fase 1 (brand.yaml uit kba-brand.json), Fase 2 ($colors/$fonts-resolver) en Fase 3
(generieke `static_elements`-renderer, `core/static_elements.py`) zijn afgerond.
RV-1 (14 hardcoded 3BM-defaults) en RV-2 (ontbrekende KBA content_styles-blokken
voor de cover) zijn daarmee opgelost — de KBA-cover (variant a) rendert nu volledig
data-gedreven, inclusief `transform: upper` op de kicker (`{kicker}` → "CONSTRUCTIEF
ADVIES", tekstextractie blijft aaneengesloten ondanks `char_space`-letterspacing).

- [ ] **RV-3** `SegoeUI-Semibold.ttf` ontbreekt in `tenants/kba/fonts/` (alleen Bold/Italic/Semilight/Regular aanwezig) — ReportLab kan `font-weight:600` niet synthetiseren, dus meta-kopjes en kicker vallen terug op Bold of Regular. User moet het bestand aanleveren.
- [ ] **RV-4** Bug: `openaec_foundation`-tenant crasht op content-secties — `KeyError: 'x'` in `heading_1()`, `core/renderer_v2.py:1317` (`n["x"]` ontbreekt). Content-secties gebruiken een afwijkend `content_styles`-schema dan waar `heading_1` vanuit gaat. Vastgelegd in `tests/baseline/FAILURES.md`, buiten scope fase 1-3.
- [ ] **RV-5** Covervarianten b (full-bleed), c (venster) en d (45°-snede) zijn niet geïmplementeerd — alleen variant a. De generieke `static_elements`-primitieven (rect/rounded_rect/line/polygon/image/text, incl. `transform`) maken ze mogelijk; nog niet uitgewerkt in `tenants/kba/brand.base.yaml`.
- [ ] **RV-6** `tenants/kba/` (nieuwe brand.yaml/brand.base.yaml/templates) is nog niet gedeployed naar `/opt/openaec/reports-tenants/kba/` op productie — `tenants/*` is gitignored, dit is een losse deploy-stap (geen deel van deze commit).

### Security — Hoog (SEC-H, 3 open)

- [ ] **SEC-H1** IDOR op `list_reports` endpoint — `GET /api/reports?project_id=X` valideert niet dat user eigenaar is. Fix: `db.get_project(project_id)` → `project.user_id == user.id` check. **Bestand:** `storage/routes.py:210-224`
- [ ] **SEC-H2** Admin endpoints missen tenant-check — admin van tenant A kan templates van tenant B wijzigen. Fix: `if user.tenant and user.tenant != tenant: raise 403` in alle admin endpoints met `{tenant}` parameter. **Bestand:** `admin/routes.py`
- [ ] **SEC-H4** `_resolve_brand_from_template()` valideert tenant niet — template kan `tenant: "andere_tenant"` bevatten → brand wordt zonder check gebruikt. Fix: na resolve matchen tegen `user.tenant`. **Bestand:** `api.py:154-168`

### Code review — Hoog (CR-H, 9 open)

- [ ] **CR-H1** Hardcoded tenant-namen in `renderer_v2.py:1910`, `template_loader.py:282`, `engine.py:83,580`, `document.py:75` — vervang door tenant-agnostische fallbacks (api.py is al gefixt via `OPENAEC_DEFAULT_BRAND`)
- [ ] **CR-H2** Cookie naam nog `bm_access_token` (oude branding). Rename naar `openaec_access_token` in gecoördineerde deploy. **Bestand:** `auth/security.py:25`
- [ ] **CR-H3** 6+ bare `except Exception: pass` — MemoryError/TypeError worden onzichtbaar ingeslikt. Fix: specifieke exceptions vangen, altijd loggen. **Bestanden:** `modules/yaml_module.py:673,802`, `template_engine.py:130,434,509`, `auth/dependencies.py:254`
- [ ] **CR-H4** God files: `api.py` (890+), `admin/routes.py` (1776+), `renderer_v2.py` (2191+). Split: api → generate/pdok/templates; admin → users/tenants/brands; renderer_v2 → builder/page_renderer/font_manager package
- [ ] **CR-H5** 10+ modules zonder tests — prioriteit `cloud.py` (security-gevoelig), `brand_api.py`, `data_transform.py`. Ook: `map_generator.py`, `title_block.py`, `base_report.py`, `visual_diff.py`, `pattern_detector.py`, `page_classifier.py`, `config_generator.py`, `diff_engine.py`, `pdf_extractor.py`, `auth/seed.py`
- [ ] **CR-H7** Geen Docker layer caching in CI — elke build ~5-10 min from scratch. Fix: `cache-from: type=gha` + `cache-to: type=gha,mode=max`. **Bestand:** `.github/workflows/ci.yml:68-76`
- [ ] **CR-H8** Frontend: Object URL memory leak bij herhaald PDF genereren — revoke is fragiel (regex replace op hash), geen unmount cleanup. Fix: raw blob URL apart bewaren, altijd revoke, cleanup in store. **Bestand:** `frontend/src/stores/apiStore.ts:182-184`
- [ ] **CR-H11** Temp files lekken bij PDOK kaart — `NamedTemporaryFile(delete=False, prefix="pdok_")` nooit opgeruimd. Fix: cleanup-mechanisme of context manager. **Bestand:** `core/template_engine.py:579-585`
- [ ] **CR-H12** SQLite connecties zonder pooling — `_get_connection()` maakt nieuwe connectie per call → "database is locked" risico. Fix: connectie-pool of thread-local, overweeg aiosqlite. **Bestand:** `storage/models.py:113-123`
- [ ] **CR-H13** `brand_api.upload_pairs` geen file size validatie — PDF uploads zonder max → disk filling. Fix: max 25 MB check. **Bestand:** `brand_api.py:359-364`

---

## 🟡 Middel

### BVP-uitbreidingen renderer_v2 (opdracht planner 28-09)
- [x] E1 heading-nummerbreedte: titel schuift op bij breder nummer (`_heading_title_x`)
- [x] E2 runs met labels in paragraph en bullet_list (`core/rich_text.py`); cellen volgen met E3
- [x] E3 celobjecten (runs, bg_color, align, ...), cell_styles, row_styles group, tabel zonder kop
- [ ] Legacy `<b>`-cellen in tabellen renderen in kba/3bm als Regular (bold-font valt terug); fix wijzigt bestaande PDF's, besluit nodig
- [x] E4 checklist-blok (1/2 kolommen, checked true/false/null)
- [x] E7 level-1 zonder paginabreuk (`page_break_before: false`, keep-with-next)
- [x] E5 definition_list
- [x] E8 part/reference in kop en inhoudsopgave
- [x] E9 statuslabel in kop (`header_label`; kba-brand heeft geen kopelementen)
- [ ] E6 SVG in image
- [ ] `tenants/kba/stationery` lokaal leeg (kop/voet/colofon ontbreken lokaal); niet zelf vullen

### Rust renderer — feature parity met Python `renderer_v2.py`

Python is live op productie, dus de Rust port hoeft niet meteen, maar zelfde gaps moeten dicht. Referentie: Python commits `e971395` en `58a2046` (10 apr).

**Heading-nummering + TOC (counter-state):**
- [ ] `Section.number: Option<String>` aan `schema.rs`
- [ ] Counter-state (`section_counter`, `subsection_counter`) in `engine.rs` — level-1 reset ontbreekt voor nested level-2
- [ ] Level-2 auto-nummering in `render_heading2` via die counter
- [ ] Resolver-logica: expliciet > auto > leeg (zie Python `_resolve_heading_number`)
- [ ] `toc.auto_number` opt-out flag voor rapporten met nummering-in-titel (bv. BBL "Afd. 4.3 — …")
- [ ] `TocBuilder::add_entry(title, level, number)` daadwerkelijk aanroepen vanuit `render_section`/`render_heading2` (nu alleen in tests)
- [ ] `TocEntry` uitbreiden met `number: Option<String>`
- [ ] Two-pass TOC rendering voor paginanummers (Python: `render_toc_to_fresh_doc` + `doc.insert_pdf(start_at=0)`)

**Renderer-bugs in Python al gefixt, Rust nog:**
- [ ] Tabel footer overflow: Y_max / bottom margin constanten matchen tenant yaml (sommige starten footer op y=768)
- [ ] HTML cell-parsing: `<b>…</b>` / `<strong>…</strong>` als full wrap → bold font
- [ ] Idempotente paginanummers (voorkomt "167"-overlay bug)
- [ ] Stationery PDF caching: voorkom re-open per pagina

### Rust Server — volgende fasen

**Phase 2 — Auth systeem**
- [ ] Auth models + security (User struct, JWT create/decode, bcrypt)
- [ ] Auth database (users, api_keys — apart `auth.db`)
- [ ] Auth middleware (AuthUser extractor: API key → JWT cookie → Bearer)
- [ ] Auth routes (`/api/auth/login`, register, logout, me, profile)
- [ ] OIDC support (JWKS fetch, RS256 validatie, auto-provisioning)
- [ ] Route protection (alle `/api/*` behalve health/auth)

**Phase 3 — Core endpoints**
- [ ] File upload (`/api/upload` — multipart, 10 MB max)
- [ ] PDOK map proxy (`/api/pdok/map`, `/api/pdok/services`)
- [ ] User profile injection in rapport colofon

**Phase 4 — Admin endpoints**
- [ ] User management CRUD
- [ ] API key management
- [ ] Organisation management
- [ ] Tenant/template/brand YAML CRUD

**Phase 5 — nice-to-haves**
- [ ] Brand onboarding wizard (of als Python microservice houden)

### Security — Middel (SEC-M, 8 open)

- [ ] **SEC-M1** CORS te permissief — `allow_methods=["*"]`, `allow_headers=["*"]`. Fix: expliciet `["GET","POST","PUT","DELETE","OPTIONS"]` + `["Content-Type","Authorization","X-API-Key"]`. **Bestand:** `api.py:104-110`
- [ ] **SEC-M2** Geen rate limiting op auth endpoints — `/api/auth/login` + `/api/auth/register` onbeperkt. Fix: `slowapi` of custom middleware. **Bestand:** `auth/routes.py`
- [ ] **SEC-M4** Exception type in API response — `type(exc).__name__` lekt in 500 responses. Fix: alleen `{"detail":"Interne serverfout"}`. **Bestand:** `api.py:257`
- [ ] **SEC-M5** Template namen path-traversal — `load("../../../etc/passwd")` — Path biedt enige bescherming maar niet waterdicht. Fix: regex `^[a-zA-Z0-9_-]+$`. **Bestanden:** `core/template_loader.py`, `core/brand.py` (`_resolve_path`)
- [ ] **SEC-M6** Frontend geen 401 interceptor — na token expiry blijft frontend "ingelogd". Fix: in `apiFetch()` 401 → uitloggen + redirect. **Bestand:** `frontend/src/services/api.ts`
- [ ] **SEC-M7** Frontend PKCE state validatie optioneel — `if (state &&` skipt check als OIDC server geen state retourneert. Fix: verplicht maken. **Bestand:** `frontend/src/components/auth/OidcCallback.tsx:41`
- [ ] **SEC-M8** Frontend localStorage restore zonder schema validatie — `as ReportDefinition` type cast zonder runtime check. Fix: Zod validatie vóór laden. **Bestand:** `frontend/src/App.tsx:57-74`
- [ ] **SEC-M9** Admin path validation onvoldoende — `_validate_path_segment()` checkt `..`/`/` maar geen `Path.resolve()` + `is_relative_to()`. Fix: post-constructie resolve-check. **Bestand:** `admin/routes.py:141-162`

### Code review — Middel (CR-M, 24 open — samengevat)

Uitgebreide lijst in commit `651b7fd` code review doc. Thematisch:

- **Frontend refactor (9 items):** ~50+ hardcoded NL strings buiten i18n (CR-M1), click-outside hook dupliceren (CR-M2), SpreadsheetEditor god component 1127 regels (CR-M3), SectionHeader/TableEditor/SpreadsheetEditor state sync bij undo/redo (CR-M4, CR-M5), `confirm()` in Backstage i.p.v. custom dialog (CR-M6), MapEditor debounce cleanup (CR-M7), unsafe `as unknown as` cast (CR-M8), hardcoded versie "0.1.0" op 2 plekken (CR-M9)
- **Schema / JSON (3 items):** `$id` + `title` nog "OpenAEC" in report.schema.json (CR-M10), draft-07 vs draft/2020-12 inconsistentie (CR-M11), `test_tenant_brand/` fixture in productie dir (CR-M13)
- **Build / config (5 items):** pyproject `pymupdf`/`httpx` duplicaten (CR-M14), ruff `line-length=100` vs CLAUDE.md `max 88` (CR-M15), geen pip caching Dockerfile (CR-M16), geen matrix test Python 3.10/3.11 (CR-M17), geen coverage report/threshold (CR-M18)
- **Python refactor (4 items):** circular import risk `cloud.py → api.py` private functies (CR-M19), `format_value()` gedupliceerd (CR-M20), CORS `allow_*=["*"]` (CR-M21, dubbelt met SEC-M1), ValueError globaal gevangen maskeert bugs (CR-M22)
- **Housekeeping (3 items):** `__init__.py` docstring nog "BM" (CR-M23), `brand_api.SESSIONS_DIR` nog `"bm_brand_sessions"` (CR-M24), geen `.env.example` in root (CR-M25)

### UX — Frontend
- [ ] **UX-1** Template wisselen behoudt bestaande content — nu gooit `loadTemplate()` alles leeg. Gewenst: secties/metadata behouden, alleen template-specifieke instellingen (format, margins, cover/colofon config) overschrijven. **Bestanden:** `frontend/src/stores/reportStore.ts`, `apiStore.ts`

### Features / templates
- [ ] **S2 Sanering template** — referentie-PDF nodig voor pixel-exacte coördinaten. Template-YAML `tenants/customer/templates/sanering.yaml`
- [ ] **#1 ERPNext integratie** — projectinfo via API keys vanuit ERPNext
- [ ] **#10 IFC read/write** — IFC bestandsformaat lezen en schrijven

### T5.9 / T5.10 — Brand onboarding wizard (Fase 3 resterend)
- [ ] **T5.9** Upload referentie PDF → coordinaten-extractie naar YAML
- [ ] **T5.10** Stationery PDF generator vanuit guidelines

### T7 — Revit Adapter (`data/revit_adapter.py` stub)
- [ ] **T7.1** `get_project_info()` — ProjectInfo uitlezing via pyRevit
- [ ] **T7.2** `get_elements()` — `FilteredElementCollector`
- [ ] **T7.3** `get_rooms()` — Room-ophaling
- [ ] **T7.4** `build_report_data()` — element data mapping naar report JSON
- [ ] **T7.5** WebSocket bridge voor live Revit → frontend push

### Desktop Tauri v2
- [ ] **D4** Authentik redirect URI `http://tauri.localhost/auth/callback`
- [ ] **D5** OA logo vervangen (huidige is Pillow-placeholder)
- [ ] **D6** Code signing (Windows Authenticode, macOS notarization) — v0.3+
- [ ] **D7** Auto-updater (`tauri-plugin-updater`) — v0.3+
- [ ] **D8** Native file dialogs (`tauri-plugin-dialog`) — v0.3+
- [ ] **D9** Offline modus / embedded Rust backend (Optie B) — v0.4+

### Deploy
- [ ] **R4.4** Rust server deploy als `report-rs.open-aec.com` (wacht op subdomein)

---

## 🟢 Laag / nice-to-have

### Code review — Laag (CR-L, 18 items — zie review-doc)

Dead code, style consistency, i18n defaults, accessibility. Niet-blokkerend. Highlights:

- **Accessibility:** geen focus-trap in dialogen (WCAG 2.1 AA) — `ShortcutHelp.tsx`, `SaveAsDialog.tsx`, `OpenDialog.tsx`
- **Dead code suspicion:** `MetadataTabs.tsx`, `RegisterPage.tsx` niet in routing — verifiëren
- **Branding resten:** `BMFlowable`, `BM_COLORS`, `BM_FONTS` class namen — package-interne rename
- **JSON Schema:** `additionalProperties: false` ontbreekt — typo's in veldnamen worden stil genegeerd
- **Security scanning:** geen Dependabot/CodeQL
- **Docstring/comments:** NL/EN inconsistentie
- Volledige lijst in code review documentatie (commit `651b7fd` warmteverlies-aanpak werkt ook hier als referentie-patroon)

### Frontend — Geavanceerde features
- [ ] **F1** Block copy/paste (Ctrl+C/V)
- [ ] **F2** Section templates (pre-filled blocks)
- [ ] **F4** Visuele template editor (drag & drop sectie volgorde)
- [ ] **F5** Revit bridge — WebSocket listener + auto-fill berekening blocks
- [ ] **F6** Undo in tekstvelden — Ctrl+Z/Y van `alwaysActive` naar `contextual` in `AppShell.tsx` (browser-native undo binnen input/textarea)

### Admin panel
- [ ] **A1** Shared sub-components verder consolideren (TenantManagement/TemplateManagement/BrandManagement gebruiken nog eigen spinners)
- [ ] **A2** API key expiry datumpicker in create-form
- [ ] **A3** Bulk operaties (meerdere keys tegelijk intrekken)

### Housekeeping
- [ ] **H1** `_temp_analyze.py` uit project root verwijderen
- [ ] **H3** `lessons_learned.md` aanmaken

### Infrastructure
- [ ] **I1** Caddyfile vereenvoudigen
- [ ] **I2** fail2ban installeren
- [ ] **I3** Portainer installeren
- [ ] **I5** Node.js 20 deprecation in CI (upgrade naar actions@v5)

### Code quality
- [ ] **Q1** Review alle store slices op dead state (vergelijkbaar met `usersError` patroon)
- [ ] **Q2** Stringly-typed user roles → `UserRole` union type
- [ ] **Q3** `formatDate` naar `frontend/src/utils/` als gedeelde utility
- [ ] **Q4** Logging in `template_loader.py` en `brand.py` (audit trail)
- [ ] **Q5** Type validatie in `TemplateConfig` constructor
- [ ] **Q6** YAML parse errors loggen i.p.v. stilletjes inslikken (`brand.py:243-244`)
- [ ] **Q7** `organisation_id` op User: ofwel volledig implementeren (org-isolatie) ofwel dead code verwijderen

### GitHub Issues — laag
- [ ] **#3** Brand visualiser — visuele editor voor brand configuratie (kleuren, fonts, layout)

### Inline TODO's in Code
- [ ] `data/revit_adapter.py:42, 64, 69, 89` — 4 methoden, onderdeel van T7

---

## 📚 Archief

- [`archief/2026-Q1-voltooid.md`](archief/2026-Q1-voltooid.md) — Q1 2026 voltooid werk (februari + maart): Rust Phase 0-6, CR-K + SEC-K security fixes, BIC Rapport 17 pagina's, OpenAEC rebranding, T5/T6/T8, cloud migratie Python + Rust, Tauri v0.2.0-alpha, 9 GitHub issues gesloten
