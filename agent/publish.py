"""Isolated direct-publish state machine. Default command is local preflight."""
import argparse
import json
import os
from pathlib import Path
import time

from .fast_operator import FastOperator
from .publish_state import Content, PublishBlocked, PublishLedger, device_lock
from .typesafe_router import TypeSafeRouter


def run_direct(op, content, ledger, task, approval, refresh):
    """The model never enters the authorization, submit or acceptance branches."""
    started = time.monotonic()
    result = {'ok': False, 'article_id': content.article, 'account_name': content.account,
              'content_digest': content.digest, 'task_id': task, 'result': 'failed_pre_submit',
              'submission_started': False, 'platform_state': 'not_submitted', 'draft_saved': False,
              'timings': {}}
    reserved = False
    boundary = False
    accepted = False
    ledger_state = None
    if approval != content.digest:
        raise PublishBlocked('Explicit approval must bind to this exact content digest')

    def phase(name, action):
        t = time.monotonic()
        try:
            return action()
        finally:
            result['timings'][name] = round(time.monotonic()-t, 3)

    try:
        # Check the ledger before waking or reading the phone.
        ledger.reserve(content, task)
        reserved = True
        ledger_state = 'prepared'
        phase('wake', op.begin)
        account = phase('account', op.account)
        if account != content.account:
            raise PublishBlocked('Account mismatch')
        phase('duplicate_check', lambda: op.check_duplicates(content))
        result['compose'] = phase('compose', lambda: op.compose_direct(content))
        if not result['compose'].get('cover_is_first') or not result['compose'].get('topics_verified'):
            raise PublishBlocked('Missing image/topic evidence')
        if refresh().digest != content.digest:
            raise PublishBlocked('Content changed since approval')
        button = phase('pre_submit', lambda: op.submit_control(content))
        ledger.transition(content, task, 'prepared', 'submitting')
        ledger_state = 'submitting'
        boundary = True
        result.update(submission_started=True, platform_state='unknown', result='unknown')
        phase('submit', lambda: op.commit_once(button))
        acceptance = phase('acceptance', lambda: op.accept_public(content))
        if (acceptance.get('privacy') != '公开可见' or not all(acceptance.get(k) for k in
            ('title_matches', 'body_matches', 'profile_card_verified', 'evidence', 'evidence_sha256'))):
            raise PublishBlocked('Incomplete public acceptance')
        result.update(acceptance, platform_state='published', result='published',
                      published_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        accepted = True
        ledger.transition(content, task, 'submitting', 'published', result)
        ledger_state = 'published'
    except Exception as exc:
        result['error'] = str(exc) if isinstance(exc, PublishBlocked) else type(exc).__name__
        if not reserved:
            result.update(result='blocked_existing_task', platform_state='not_checked')
        if accepted:
            result['ledger_error'] = 'Publication accepted but ledger write failed; reconcile only'
    finally:
        if reserved:
            try:
                result['phone_screen_off'] = phase('cleanup', op.cleanup_verified)
            except Exception:
                result['phone_screen_off'] = False
            if accepted and not result['phone_screen_off']:
                # Publication is still known; only cleanup failed. Never post again.
                result['result'] = 'published_cleanup_failed'
            result['ok'] = accepted and result['phone_screen_off'] and not result.get('ledger_error')
            try:
                ledger.transition(content, task, ledger_state,
                                  'published' if accepted else 'unknown' if boundary else 'failed_pre_submit', result)
            except Exception:
                result['ledger_error'] = 'Final ledger write failed; preserve receipt and reconcile only'
                result['ok'] = False
        result['metrics'] = dict(op.metrics)
        result['complete_workflow_seconds'] = round(time.monotonic()-started, 3)
    return result


def parser():
    p = argparse.ArgumentParser(description='XHS isolated direct publisher v2; no draft intermediate')
    p.add_argument('command', choices=('preflight', 'publish', 'probe'))
    p.add_argument('article', nargs='?')
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--state-dir', type=Path, default=Path.home()/'.local/share/xhs-phone-agent/publish-v2')
    p.add_argument('--task-id')
    p.add_argument('--approve-digest', help='User-approved exact preflight digest; never auto-populate')
    p.add_argument('--allow-typesafe-text', action='store_true')
    p.add_argument('--typesafe-budget-usd', type=float, default=.01)
    return p


def main():
    os.umask(0o077)
    args = parser().parse_args()
    if args.command != 'probe' and not args.article:
        raise SystemExit('Article ID required')
    router = TypeSafeRouter(allowed=args.allow_typesafe_text, budget_usd=args.typesafe_budget_usd)
    op = FastOperator(args.config, router)
    if args.command == 'preflight':
        content = Content.load(op, args.article)
        print(json.dumps({'article_id': content.article, 'title': content.title,
                          'images': len(content.images), 'approval_digest': content.digest,
                          'phone_touched': False}, ensure_ascii=False, indent=2))
        return
    if args.command == 'publish' and (not args.task_id or not args.approve_digest):
        raise SystemExit('Explicit task ID and approval digest required')
    serial = op.require_device()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with device_lock(serial):
        if args.command == 'probe':
            started = time.monotonic()
            result = {'mode': 'read_only_profile_probe', 'ok': False, 'published': False}
            try:
                op.begin()
                op.account()
                op.dump_ui(force=True)
                reads = op.metrics['ui_reads']
                for rid in ('index_me', 'profileSearchEntrance', 'index_post'):
                    op.find_nodes(resource_id='com.xingin.xhs:id/'+rid)
                result.update(ok=True, checks=3, additional_ui_reads=op.metrics['ui_reads']-reads)
            except Exception as exc:
                result['error'] = str(exc) if isinstance(exc, PublishBlocked) else type(exc).__name__
            finally:
                try: result['phone_screen_off'] = op.cleanup_verified()
                except Exception: result['phone_screen_off'] = False
                result['ok'] = result['ok'] and result['phone_screen_off']
                result['metrics'] = op.metrics
                result['complete_workflow_seconds'] = round(time.monotonic()-started, 3)
        else:
            content = Content.load(op, args.article)
            ledger = PublishLedger(args.state_dir/'ledger.sqlite3')
            try:
                result = run_direct(op, content, ledger, args.task_id, args.approve_digest,
                                    lambda: Content.load(op, args.article))
            finally:
                ledger.close()
            # Durable receipts are an outbox. Core integration must compare digest,
            # upload proof and acknowledge before marking a server revision published.
        result['typesafe'] = {'calls': router.calls, 'metrics': router.metrics}
        receipt = args.state_dir/('probe-'+str(time.time_ns())+'.json' if args.command == 'probe'
                                  else 'result-'+content.digest+'.json')
        if receipt.exists():
            raise PublishBlocked('Receipt already exists; preserve existing result')
        with receipt.open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(result, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        print(json.dumps({**result, 'receipt': str(receipt)}, ensure_ascii=False, indent=2))
        if not result.get('ok'):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
