# UI-TARS recovery contract

Use this recovery layer only after the deterministic ADB operator cannot find a required text,
resource ID, or ordinary control.

## Recovery order

1. Capture the screenshot, UI tree, current focus, stable error signature, and page fingerprint.
2. Stop immediately on login, CAPTCHA, account anomaly, policy warning, or an ambiguous submission
   result.
3. Reuse an experience only when `failure_kind`, target, and page fingerprint all match.
4. If no verified experience matches:
   - with `recovery_provider: codex`, write a durable Codex takeover item and stop;
   - with `recovery_provider: ui_tars`, ask the configured visual model for at most 1–5 allowlisted
     recovery actions.
5. Resume the deterministic operator only after the missing target is visible again. Never treat
   a model action as proof that a draft, schedule, or publication succeeded.

## Action boundary

Allow only `tap_text`, `tap_resource`, `tap_point`, `swipe`, `back`, `wait`, and `stop`.
Reject actions that touch or mention:

- public or scheduled publication;
- deletion;
- login or CAPTCHA;
- payment;
- privacy changes;
- account or policy warnings.

The recovery target is navigation restoration, not completion of the business action.

## Learning and reuse

- Learning covers every phone operation and interface, including restricted screens. This broader
  observation boundary does not broaden execution authority.
- Record verified workflows with `scripts/record_phone_experience.py`. Restricted workflows are
  stored as `observe_locate_prompt_validate_only` and never authorize the final restricted tap.
- Save every failure as an incident with before-state evidence.
- Save a successful new recovery as `candidate`.
- Promote it to `approved` only after two independent deterministic confirmations that the target
  became visible.
- Move an experience to `quarantined` after its first failed reuse.
- Block new draft, schedule, or publish work while a Codex takeover item remains pending.
- Never let an experience override content, account, schedule, visibility, duplicate, or
  post-publication acceptance gates.

## Model and privacy

Use `~/.config/codex/xhs-ui-tars.json` or set `UI_TARS_CONFIG`.
Copy the shape from `assets/xhs_ui_tars.example.json`.

- Keep `recovery_provider: codex` for the default Codex takeover queue.
- Set `recovery_provider: ui_tars` and `enabled: true` to let a UI-TARS-compatible
  OpenAI-style vision endpoint propose guarded recovery actions.
- Local or LAN endpoints may run without an API key. Read cloud keys only from the configured
  environment variable.
- Do not upload phone screenshots to a non-local endpoint unless `allow_cloud: true` is explicitly
  configured for that run.

Inspect without executing model actions:

```bash
scripts/xhs observe --no-model
scripts/xhs recovery-status
```

Always finish observation, recovery, failure, and success with verified screen-off cleanup.
