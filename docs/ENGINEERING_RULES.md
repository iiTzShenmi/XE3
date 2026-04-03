# XE3 Engineering Rules

This file records the architecture and maintenance rules we expect future changes to follow.
It exists so we can re-review against the same baseline later instead of relying on memory.

## 0. Critical Reminder For Future Codex
- If you extract helpers into a new module, finish the wiring in the same round.
- Replace old call sites and remove dead duplicate helpers before you stop.
- Before pushing, always do all three:
  1. `py_compile` for touched modules
  2. restart the relevant service
  3. update `log/XE3_PUSH_LOG.md`

## 1. Git and Runtime Must Stay Aligned
- Keep the running bot and Git history as close as possible.
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

## 5. Exceptions Must Stay Actionable
- Catch specific exception classes where possible.
- If a broad catch is still necessary, it must log context with `logger.exception(...)`.
- Do not silently swallow parser/runtime failures that would hide schema drift.

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

## 8. Review Checklist For Future Changes
Before considering a refactor complete, verify:
- metadata-driven selectors still work
- reminder worker still starts cleanly
- direct file delivery still works for small files
- Cloudflare/proxy fallback still works for large files
- `py_compile` passes for touched Python modules
- the push log has been updated
