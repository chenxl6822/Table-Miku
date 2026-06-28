# Table-Miku Monitoring and Knowledge Upgrade Progress

> Started: 2026-06-28  
> Scope: weather monitoring, system/network monitoring, trusted knowledge sources, SQLite knowledge entry points  
> Safety boundary: only modify files under `D:\AIWorkspace\projects\Table-Miku`. `D:\AIWorkspace\projects\Obsidian Vault` is read-only only.

## Baseline

- `.\.venv\Scripts\python.exe -m compileall main.py table_miku`: passed.
- `.\.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.tmp_pytest`: 135 passed.
- Existing architecture:
  - Weather: `table_miku/weather.py`, `table_miku/weather_monitor.py`.
  - System monitor: `table_miku/system_monitor.py`.
  - Knowledge JSON path: `table_miku/knowledge_base.py`.
  - Knowledge SQLite path: `table_miku/knowledge_db.py`, `table_miku/knowledge_repository.py`.
  - GUI still reads JSON knowledge in several places.

## Decisions

- Weather location strategy: hybrid. Manual address and coordinate input first, geocoder cache second, IP auto-location only as low-confidence fallback.
- Knowledge source strategy: trusted sources first. Official docs, RFCs, standards, papers, and read-only Obsidian notes outrank Wikipedia and offline seeds.
- Progress tracking: this single document is updated after every implementation phase.

## Phase Log

### Phase 0 - Progress Document

- Status: complete.
- Changes:
  - Created this live progress document.
- Tests:
  - Covered by the Phase 1 full test run.
- Next:
  - Harden weather location resolution, cache, forecast, and weather alert evaluation.

### Phase 1 - Weather Location and Alerts

- Status: complete.
- Changes:
  - Added manual coordinate parsing for `lat,lon` and labeled `lat=..., lon=...` inputs.
  - Added local geocoder cache with stale-cache fallback for manual city/address lookups.
  - Marked IP auto-location as low-confidence and added explicit user-facing source notes.
  - Requested Open-Meteo wind speed in `m/s` and added optional hourly forecast data.
  - Added current and near-future weather alert evaluation for rain, snow, freezing rain, fog, thunderstorm, high/low temperature, and wind.
  - Updated `WeatherMonitor` to read interval/cooldown/lead time from settings and use hourly alerts.
- Tests:
  - `.\.venv\Scripts\python.exe -m compileall main.py table_miku`: passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\test_weather_monitoring.py -q --basetemp=.tmp_pytest`: 7 passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.tmp_pytest`: 142 passed.
- Risks:
  - Weather location cache uses app data storage and is intentionally local-only.
  - Open-Meteo hourly alerts are forecast-based, not government severe-weather warnings.
- Next:
  - Harden system memory and layered network monitoring to reduce false positives.

### Phase 2 - System, Memory, and Network Monitoring

- Status: complete.
- Changes:
  - Extended network probe results with DNS, TCP, TLS, HTTP status, host, port, and error kind fields while preserving the previous fields.
  - Split network probing into URL validation, DNS resolution, TCP connect, optional TLS handshake, and HTTP status fetch.
  - Added error classification for invalid URL, DNS, TCP, TLS, timeout, HTTP status, and generic network errors.
  - Added consecutive network failure threshold and recovery reporting to reduce one-off false positives.
  - Added memory pressure detection using both used percentage and available MB thresholds.
  - Added default settings for `memory_available_warning_mb` and `network_warning_checks`.
- Tests:
  - `.\.venv\Scripts\python.exe -m compileall main.py table_miku`: passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\test_system_monitoring.py -q --basetemp=.tmp_pytest`: 6 passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.tmp_pytest`: 148 passed.
- Risks:
  - Direct TCP/TLS probes intentionally expose which layer failed, but environments using unusual HTTP-only proxies may need manual interpretation.
- Next:
  - Add trusted knowledge source metadata and read-only Obsidian ingestion.

### Phase 3 - Trusted Knowledge Sources

- Status: complete.
- Changes:
  - Added trusted source metadata with source priority: official/RFC/standard/paper > Obsidian read-only > Wikipedia > offline.
  - Added read-only Obsidian Markdown adapter that skips hidden and sensitive paths.
  - Added trusted ingest service that writes summaries, metadata, source records, chunks, and review states into Table-Miku SQLite only.
  - Added configurable `knowledge.trusted_sources.obsidian_vault` without hardcoding the user's local Vault path.
- Tests:
  - `.\.venv\Scripts\python.exe -m compileall main.py table_miku`: passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\test_trusted_knowledge_sources.py -q --basetemp=.tmp_pytest`: 5 passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.tmp_pytest`: 153 passed.
- Risks:
  - Obsidian content is imported as local summaries/chunks only; the original Vault remains untouched.
- Next:
  - Route knowledge UI, review, and assistant context through SQLite Repository with JSON fallback.

### Phase 4 - Unified Knowledge Entry Points

- Status: complete.
- Changes:
  - Added `knowledge_service.py` as the unified SQLite-first knowledge entry point.
  - Seeded missing default topics into SQLite with JSON migration/fallback compatibility.
  - Added Repository readers for card chunks and linked sources.
  - Routed app knowledge library, today's task knowledge block, review dialog, assistant context, and reminder review summary through the new service.
  - Kept old JSON knowledge/review modules as fallback paths and for backward-compatible tests.
  - Fixed an existing runtime issue where `app.py` called `format_knowledge` without importing it.
- Tests:
  - `.\.venv\Scripts\python.exe -m compileall main.py table_miku`: passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\test_knowledge_service.py -q --basetemp=.tmp_pytest`: 5 passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.tmp_pytest`: 158 passed.
- Risks:
  - First runtime use may initialize or migrate `data/knowledge.db`; this is intended app data under Table-Miku, not the Obsidian Vault.
- Next:
  - Update user-facing docs and run final validation.

### Phase 5 - Documentation and Final Validation

- Status: complete.
- Changes:
  - Updated `README.md` with weather location modes, layered network monitoring, trusted knowledge source priority, and Obsidian read-only configuration.
  - Updated `TableMiku_STATUS.md` with the 2026-06-28 monitoring and trusted knowledge upgrade summary.
  - Kept this progress document current through every phase.
- Tests:
  - `.\.venv\Scripts\python.exe -m compileall main.py table_miku`: passed.
  - `.\.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.tmp_pytest`: 158 passed.
- Final status:
  - Implementation complete for the planned phases.
  - No writes were made to `D:\AIWorkspace\projects\Obsidian Vault`.
