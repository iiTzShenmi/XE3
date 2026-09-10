# XE3 Engineering Rules

This file records the architecture and maintenance rules we expect future changes to follow.
It exists so we can re-review against the same baseline later instead of relying on memory.

## 0. Critical Reminder For Future Codex
- If you extract helpers into a new module, finish the wiring in the same round.
- Replace old call sites and remove dead duplicate helpers before you stop.
- When a helper already exists in `common.py` or a dedicated module, import it instead of cloning a new local copy.
- Before pushing, always do all three:
  1. `py_compile` for touched modules
  2. restart the relevant service
  3. update `log/XE3_PUSH_LOG.md`

## 1. Git and Runtime Must Stay Aligned
- Keep the running bot and Git history as close as possible.
- After rebuilding or replacing the virtual environment, restart every XE3 user service. A still-running process can retain deleted packages and hide missing runtime dependencies until the next reboot.
- Runtime imports such as WSGI servers must be declared in `requirements.txt`; test-only packages belong in `requirements-dev.txt`.
- After dependency or proxy changes, test both local `/healthz` and one short-lived public file-proxy download through Cloudflare.
- After each stable chunk of work:
  1. run syntax/runtime validation
  2. commit the change
  3. append a matching entry to `log/XE3_PUSH_LOG.md`
  4. push only after the service has been sanity-checked
- Avoid leaving hotfixes uncommitted for long periods.

## 2. Prefer Structured Metadata Over String Guessing
- Selector, dropdown, and summary behavior must be driven by structured `xe3_meta` payload metadata.
- Do not infer behavior from human-facing embed text unless handling legacy payloads.
- Text is for users. Metadata is for program logic.
- Never hardcode an E3 semester in runtime paths or scraper filters. Derive the current semester from Taipei time, and only report a course count after applying that semester filter.
- Treat `courses_current.json` as the authoritative enrollment index. Same-semester runtime folders that are no longer indexed must not reappear in user responses.

## 3. Keep Module Responsibilities Narrow
- `agent/features/e3/handler.py` should mainly route commands and coordinate modules.
- Data shaping belongs in feature modules such as course/timeline/file helpers.
- Discord UI rendering belongs in `agent/platforms/discord/*` modules, not E3 handlers.
- Reminder scheduling, reminder payload formatting, and periodic sync logic should live in separate modules.

## 4. Keep Discord UX Consistent
- Prefer editing the existing bot message for interactive button/select flows.
- Use new messages only when there is a real new artifact to deliver:
  - file uploads
  - reminder/test notifications
  - fallback delivery cases
- Keep responses clean, short, and readable.
- Maintenance/admin commands must be owner-only and must not broadcast operational output to normal users unless that behavior is explicitly intended.

## 4.1 Components v2 Safety
- Components v2 messages must use a `discord.ui.LayoutView` and must not include legacy `content` or `embeds` in the same send/edit request.
- When converting an existing message to Components v2, explicitly clear `content`, `embeds`, and `attachments` in that edit.
- The Components v2 message flag is irreversible. Every later page, error, empty state, and back-navigation edit for that message must also render through the v2 path.
- Keep the legacy Embed sender only as a separate path for artifacts that intentionally use it, such as a new file-delivery message. Never use it to edit an existing v2 message.
- Validate the 4,000-character and 40-component limits before sending a `LayoutView`.

## 5. Exceptions Must Stay Actionable
- Catch specific exception classes where possible.
- If a broad catch is still necessary, it must log context with `logger.exception(...)`.
- Do not silently swallow parser/runtime failures that would hide schema drift.
- Authentication or dashboard-fetch failure must never fall back to stale cache and report a successful sync.

## 5.1 Runtime Data Safety
- Write scraper JSON through an atomic temporary-file replacement.
- Validate fetched sections before exposing them to commands or reminders.
- Quarantine malformed section files; fail the whole sync when the current course index is invalid.
- A partial endpoint failure may retain the last valid section, but `/e3 status` must report the sync as partial.

## 5.2 E3 Upload Safety
- Keep upload commands owner-only until multiple real assignments have passed preflight, upload, save, and verification tests.
- Run `/e3 uploadcheck` before the first upload to a new assignment configuration. Preflight must remain read-only.
- Never delete an existing submission before a replacement file has been safely staged and verified. Keep automatic replacement disabled until its exact Moodle flow has a reviewed HAR and regression tests.
- Treat `savesubmission` and "submit for grading" as separate states. Never report success while Moodle still shows a draft or requires a submission statement.
- Every upload attempt must have an operation ID and stage logs without cookies, `sesskey`, file content, or repository response bodies.
- All E3 upload HTTP calls must use bounded connect/read timeouts and return an actionable stage-specific error.
- Parse repository upload responses as JSON. Do not infer failure by searching arbitrary response text for words such as `error`.
- Normalize filenames before logging, queuing, or uploading. Queued directories and files must use owner-only permissions and be removed after terminal success.
- HAR files are sensitive credentials captures. Keep them under ignored runtime data with mode `0600`, never commit them, and remove them when the flow has been documented.

## 6. Refactors Must Be Incremental
- Split large files in stages.
- Do not combine architectural refactors with unrelated product behavior changes in one commit unless necessary.
- Each phase should leave the bot bootable and the core flows working:
  - `/e3 course`
  - `/e3 timeline`
  - `/e3 files`
  - `/e3 remind`
  - direct file delivery

## 7. Shared Formatting Rules
- User-facing copy should favor Traditional Chinese unless a strong reason exists otherwise.
- Keep output concise, readable, and mobile-friendly.
- Use separators/whitespace intentionally; avoid long unbroken blocks.
- Keep `/chksys` limited to machine/OS health. XE3 account, scraper, cache, validation, and reminder-worker diagnostics belong in `/e3 status`.

## 7.1 Sync Concurrency
- The legacy scraper mutates module-level runtime paths. Never run multiple accounts concurrently in threads within one process.
- Use bounded spawn processes for cross-account concurrency and a cross-process per-user file lock to prevent duplicate syncs for the same account.
- Keep the default worker count conservative (`2`) to avoid overloading E3 even when the host has more CPU capacity.

## 8. Review Checklist For Future Changes
Before considering a refactor complete, verify:
- metadata-driven selectors still work
- reminder worker still starts cleanly
- direct file delivery still works for small files
- Cloudflare/proxy fallback still works for large files
- `py_compile` passes for touched Python modules
- the push log has been updated
