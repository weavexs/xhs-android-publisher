---
name: xhs-android-publisher
description: Safely control one USB-debugged Android device through ADB to prepare, validate, save drafts, schedule, or publicly publish Xiaohongshu image posts. Use when Codex needs to send 小红书笔记 from local title/body/image manifests, inspect device readiness, preserve image order, verify the target account and public visibility, or package a repeatable Android publishing flow. Enforce explicit publish authorization, no blind retries, and screen-off cleanup after every outcome.
---

# Xiaohongshu Android publisher

Operate through the bundled deterministic ADB scripts. Treat public publication as an external,
state-changing action. Never infer permission to publish from permission to inspect or save a draft.

## Load only what is needed

- Read [references/setup.md](references/setup.md) before first use on a Mac or Android device.
- Read [references/article-schema.md](references/article-schema.md) when creating or diagnosing an
  article manifest.
- Run scripts directly; do not load `scripts/xhs_operator.py` into context unless debugging or
  adapting to a changed Xiaohongshu UI.

## Hard gates

1. Record the workflow start time before the first check.
2. Require an explicit user request to publish publicly, or an exact same-day schedule plus an
   independently verified `ready` article. Saving a draft is not publication permission.
3. Require exactly one authorized ADB device, the configured screen size, Xiaohongshu, the bundled
   input helper, the expected account, and an unlocked usable app state.
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

## First-use setup

Follow [references/setup.md](references/setup.md), then run:

```bash
scripts/xhs prepare
scripts/xhs doctor
```

Do not continue unless `doctor` returns `ok: true`. The current UI coordinate fallback is validated
only for the configured `ONEPLUS A6003` at `1080x2280`. A different model or screen size is a
blocker until the operator is adapted and revalidated.

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
- `privacy=公开可见` only when proven
- `phone_screen_off=true`

If `phone_screen_off` is false, report the cleanup failure explicitly and do not call the task fully
complete.
