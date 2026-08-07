---
name: xhs-android-publisher
description: Safely control one USB-debugged Android device through ADB to prepare, validate, save drafts, schedule, or publicly publish Xiaohongshu image posts, with guarded UI-TARS/Codex takeover for recoverable UI drift and verified experience reuse. Use when Codex needs to send 小红书笔记, inspect device readiness, preserve image order, verify account and visibility, recover missing controls, or package a repeatable Android publishing flow. Enforce explicit publish authorization, no blind retries, and screen-off cleanup.
---

# Xiaohongshu Android publisher

Operate through the bundled deterministic ADB scripts. Treat public publication as an external,
state-changing action. Never infer permission to publish from permission to inspect or save a draft.

## Load only what is needed

- Read [references/setup.md](references/setup.md) before first use on a Mac or Android device.
- Read [references/article-schema.md](references/article-schema.md) when creating or diagnosing an
  article manifest.
- Read [references/ui-tars-recovery.md](references/ui-tars-recovery.md) before enabling UI-TARS,
  handling a pending takeover, or changing recovery policy.
- Run scripts directly; do not load `scripts/xhs_operator.py` into context unless debugging or
  adapting to a changed Xiaohongshu UI.

## Hard gates

1. Record the workflow start time before the first check.
2. Require an explicit user request to publish publicly, or an exact same-day schedule plus an
   independently verified `ready` article. Saving a draft is not publication permission.
3. Require exactly one authorized USB ADB device matching the configured model, screen size,
   Xiaohongshu, input helper, expected account, and an unlocked usable app state. Ignore unrelated
   network ADB devices; never let them enter the operation or UI-TARS recovery path.
4. Validate `发布内容.json`, body text, 1–6 ordered PNG files, dimensions, title length, body length,
   and `status: ready`.
5. Do not publish when the article is already `published`; classify it as a duplicate blocker.
6. Before tapping publish, compare the compose page title, body, and visible image count against the
   local manifest.
7. After upload, require the account homepage card and detail-page title, body, and `公开可见`.
   Upload completion alone is not success.
8. On CAPTCHA, login, account mismatch, policy warning, network ambiguity, missing postcheck, or any
   unknown outcome: stop, preserve evidence, keep the article `ready`, and do not retry.
9. End every success, failure, or skip with screen-off cleanup. Require
   `phone_screen_off: true`; never claim completion otherwise.
10. Let UI-TARS recover only missing ordinary controls or navigation drift. Never let it execute or
    authorize publish, schedule, delete, login, CAPTCHA, privacy, account-policy, duplicate, or
    post-publication acceptance decisions.

## First-use setup

Follow [references/setup.md](references/setup.md), then run:

```bash
scripts/xhs prepare
scripts/xhs doctor
```

Do not continue unless `doctor` returns `ok: true`. The current UI coordinate fallback is validated
only for the configured `ONEPLUS A6003` at `1080x2280`. A different model or screen size is a
blocker until the operator is adapted and revalidated.

## UI-TARS recovery

Keep deterministic ADB logic primary. On a missing text/resource/control, the operator records an
incident and tries one guarded recovery for that target:

1. reuse an exact matching approved/candidate experience;
2. otherwise queue Codex takeover by default, or call a configured UI-TARS vision endpoint;
3. resume only after the original target is visible and deterministically verified.

Do not poll the takeover queue at a fixed interval. Check it at workflow start and when a failure is
recorded. A pending takeover blocks new draft, schedule, and publish work.

Run:

```bash
scripts/xhs observe --no-model
scripts/xhs recovery-status
```

New recovery paths remain candidates until two independent successes. Quarantine a path after its
first failed reuse. Follow [references/ui-tars-recovery.md](references/ui-tars-recovery.md) for the
action whitelist, cloud screenshot boundary, and experience lifecycle.

## Draft workflow

Use this only when the user asks to create or replace a phone draft:

```bash
scripts/xhs article-check <article_id>
scripts/xhs article-draft <article_id>
scripts/xhs end
```

Require a verified draft title and screenshot. Report “草稿已保存”; do not report “已发布”.
Never use `--replace-existing` without explicit permission to replace the matching draft.

## Public publishing workflow

Use the guarded entrypoint:

```bash
scripts/safe_publish.py <article_id>
```

Do not invoke raw `article-publish` for routine work. `safe_publish.py` runs doctor and article
preflight, delegates the tested publish flow, captures total time, and verifies screen-off cleanup.
It never retries.

Accept success only when the result includes all of:

- `ok: true`
- expected `account_name`
- exact title/body acceptance
- `privacy: 公开可见`
- `phone_screen_off: true`

## Native scheduled publishing

Only schedule when the manifest contains an explicit future `scheduled_at` and the user authorized
that exact time:

```bash
scripts/xhs article-check <article_id>
scripts/xhs article-schedule <article_id>
scripts/xhs end
```

Verify the native date/time label before submission. Do not substitute another date or time.

## Final report

Always report:

- result: `published`, `draft_saved`, `scheduled`, `skipped`, or `failed_unverified`
- article ID, expected account, title, and output evidence paths
- complete workflow elapsed time, including retries or rework
- operator elapsed time when available
- UI-TARS recovery source, incident/experience path, and pending takeover state when applicable
- `privacy=公开可见` only when proven
- `phone_screen_off=true`

If `phone_screen_off` is false, report the cleanup failure explicitly and do not call the task fully
complete.
