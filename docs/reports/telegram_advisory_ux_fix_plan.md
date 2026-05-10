# Plan: fix Telegram advisory / contrast (operator-readable)

## Goals

- Contrast messages: no raw `state_machine_contrast`; plain “alert vs live state mismatch” + 3 human actions.
- No broken Markdown (`linear\_extrapolation`).
- Do not scare with URGENT/CRITICAL + catastrophic forecast when SDK evidence shows healthy pod; Telegram render uses a **sanitized copy**; CRAT/audit keeps **original** LLM `AnalystAdvisory` (document in code comment).

## Work order

1. **[src/workers/evidence_consumer.py](src/workers/evidence_consumer.py)** — `t_msg` (~1684–1690): new ROOT CAUSE sentence; `ACTION (human):` bullets (Prom/AM labels; silence/fix rule).
2. **[src/workers/telegram_advisory_emitter.py](src/workers/telegram_advisory_emitter.py)** — normalize `\_` → `_` in model-sourced strings before Markdown escape; unit test.
3. **`clone + sanitize` before `render_advisory_to_telegram`** — in caller ([src/workers/evidence_consumer.py](src/workers/evidence_consumer.py) near `render_advisory_to_telegram`): if heuristic “healthy PASSED + verdict URGENT|CRITICAL + root_cause mentions low usage”, downgrade `verdict` to `INVESTIGATE`, clip forecast severities or replace with one-line “suppressed extreme forecast — evidence healthy”; pass clone to Telegram only.
4. **Prompt** — `build_advisory_system_prompt` (locate via grep in `advisory_analyst_handler` or split module): explicit rule — no CRITICAL/CATASTROPHIC forecast when batch shows PASSED/healthy.
5. **Verify** — `pytest` on touched modules; optional `scripts/e2e_one_alert_full_advisory_path.sh` on lab.

## Out of scope

- CRAT order, `kafka_evidence_loop`, gateway imports.
- Full Vietnamese i18n (optional later: `OMNI_TELEGRAM_LOCALE`).
