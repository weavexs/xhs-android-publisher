import json
import struct
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.fast_operator import FastOperator, fingerprint
from agent.publish import run_direct, parser
from agent.publish_state import Content, PublishLedger, PublishBlocked, device_lock, sha
from agent.typesafe_router import TypeSafeRouter, public_controls
from scripts.xhs_operator import UiNode, Article


def content():
    return Content('test-account', 'T001', 'A test title', 'test body #test', ('test',), (), (), 'digest-v1')


class Phone:
    """No real device/network; counts every consequential action."""
    def __init__(self, fail=None, off=True):
        self.fail, self.off, self.actions, self.metrics = fail, off, [], {}
    def event(self, name):
        self.actions.append(name)
        if name == self.fail: raise PublishBlocked('simulated '+name)
    def begin(self): self.event('begin')
    def account(self): self.event('account'); return 'test-account'
    def check_duplicates(self, c): self.event('duplicates')
    def compose_direct(self, c): self.event('compose'); return {'cover_is_first': True, 'topics_verified': True}
    def submit_control(self, c): self.event('preflight'); return 'button'
    def commit_once(self, b): self.event('commit')
    def accept_public(self, c):
        self.event('accept')
        return {'privacy': '公开可见', 'title_matches': True, 'body_matches': True,
                'profile_card_verified': True, 'evidence': 'test.png', 'evidence_sha256': 'testhash'}
    def cleanup_verified(self): self.event('cleanup'); return self.off


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = PublishLedger(Path(self.temp.name)/'test.db')
    def tearDown(self): self.ledger.close(); self.temp.cleanup()
    def run_flow(self, phone, c=None, task='job', refresh=None):
        c = c or content()
        return run_direct(phone, c, self.ledger, task, c.digest, refresh or (lambda:c))
    def test_direct_once_no_draft_account_once(self):
        p=Phone(); r=self.run_flow(p)
        self.assertTrue(r['ok']); self.assertFalse(r['draft_saved'])
        self.assertEqual(p.actions, ['begin','account','duplicates','compose','preflight','commit','accept','cleanup'])
    def test_duplicate_blocks_before_wake_even_new_task_and_version(self):
        self.run_flow(Phone())
        p=Phone(); r=self.run_flow(p,replace(content(),digest='v2'),'another-task')
        self.assertFalse(r['ok']); self.assertEqual(p.actions, [])
    def test_auth_exact_version_before_any_side_effect(self):
        p=Phone()
        with self.assertRaises(PublishBlocked): run_direct(p,content(),self.ledger,'job','wrong',lambda:content())
        self.assertEqual(p.actions, [])
    def test_changed_copy_prevents_commit(self):
        p=Phone(); r=self.run_flow(p,refresh=lambda:replace(content(),digest='changed'))
        self.assertFalse(r['submission_started']); self.assertNotIn('commit',p.actions)
    def test_disconnect_at_commit_durable_unknown_and_no_replay(self):
        p=Phone('commit'); r=self.run_flow(p)
        self.assertTrue(r['submission_started']); self.assertEqual(r['platform_state'],'unknown')
        self.ledger.close(); self.ledger=PublishLedger(Path(self.temp.name)/'test.db')
        p2=Phone(); self.run_flow(p2,task='new-job')
        self.assertEqual(p2.actions,[])
    def test_missing_postcheck_never_says_published(self):
        r=self.run_flow(Phone('accept')); self.assertEqual(r['result'],'unknown')
    def test_screen_failure_does_not_erase_known_publication(self):
        r=self.run_flow(Phone(off=False))
        self.assertFalse(r['ok']); self.assertEqual(r['platform_state'],'published')
        self.assertEqual(r['result'],'published_cleanup_failed')
    def test_pre_submit_failure_cleanup_no_commit(self):
        p=Phone('compose'); r=self.run_flow(p)
        self.assertEqual(r['result'],'failed_pre_submit'); self.assertEqual(p.actions[-1],'cleanup')
        self.assertNotIn('commit',p.actions)
    def test_account_mismatch_before_upload(self):
        p=Phone(); p.account=lambda:'wrong-account'
        r=self.run_flow(p); self.assertFalse(r['ok']); self.assertNotIn('compose',p.actions)
    def test_partial_acceptance_is_unknown(self):
        p=Phone(); p.accept_public=lambda c:{'privacy':'公开可见'}
        r=self.run_flow(p); self.assertEqual(r['result'],'unknown')
    def test_two_accounts_not_mixed(self):
        self.run_flow(Phone())
        p=Phone(); p.account=lambda:'second-account'
        self.assertTrue(self.run_flow(p,replace(content(),account='second-account'),'other')['ok'])
    def test_device_lock(self):
        with device_lock('synthetic',Path(self.temp.name)):
            with self.assertRaises(PublishBlocked):
                with device_lock('synthetic',Path(self.temp.name)): pass
    def test_submit_boundary_is_atomic(self):
        c=content(); self.ledger.reserve(c,'job'); self.ledger.transition(c,'job','prepared','submitting')
        with self.assertRaises(PublishBlocked): self.ledger.transition(c,'job','prepared','submitting')
    def test_parser_requires_explicit_command(self):
        self.assertEqual(parser().parse_args(['preflight','T001','--config','test.json']).command,'preflight')
    def test_failed_ledger_before_commit_never_clicks(self):
        p=Phone()
        with patch.object(self.ledger,'transition',side_effect=OSError('disk unavailable')):
            r=self.run_flow(p)
        self.assertNotIn('commit',p.actions); self.assertEqual(p.actions[-1],'cleanup')
        self.assertFalse(r['ok']); self.assertIn('ledger_error',r)
    def test_failed_ledger_after_acceptance_preserves_known_publication(self):
        transition=self.ledger.transition
        def fail(c,t,expected,target,evidence=None):
            if target=='published': raise OSError('disk unavailable')
            return transition(c,t,expected,target,evidence)
        p=Phone()
        with patch.object(self.ledger,'transition',side_effect=fail): r=self.run_flow(p)
        self.assertFalse(r['ok']); self.assertEqual(r['platform_state'],'published')
        self.assertEqual(p.actions[-1],'cleanup')
        p2=Phone();self.run_flow(p2,task='new');self.assertEqual(p2.actions,[])


class RouterTests(unittest.TestCase):
    def nodes(self):
        return [UiNode('', 'private-id', '消息，有私人消息', True, (0,0,10,10)),
                UiNode('我', 'profile', '', True, (10,0,20,10)),
                UiNode('发布', 'commit', '', True, (20,0,30,10))]
    def response(self,payload):
        options=payload['questions']['target']['criteria']
        return {'model':'jev-1.13.0','answers':{'page':{'type':'choice','choice':'home'},
                'target':{'type':'choice','choice':'n1','confidence':.98,
                          'probabilities':{k:1. if k=='n1' else 0. for k in options}}},'usage':{'input_tokens':100}}
    def test_closed_candidates_privacy_and_batching(self):
        requests=[]
        def send(p): requests.append(p); return self.response(p)
        r=TypeSafeRouter(allowed=True,transport=send)
        self.assertEqual(r.select(self.nodes(),'profile','fp'),1)
        self.assertEqual(set(requests[0]['questions']),{'page','target'})
        wire=json.dumps(requests,ensure_ascii=False)
        for forbidden in ('私人','消息','private-id','commit','发布'):
            self.assertNotIn(forbidden,wire)
    def test_same_fingerprint_cached(self):
        r=TypeSafeRouter(allowed=True,transport=self.response)
        r.select(self.nodes(),'profile','fp');r.select(self.nodes(),'profile','fp')
        self.assertEqual(r.calls,1)
    def test_public_commit_never_model_target(self):
        r=TypeSafeRouter(allowed=True,transport=self.response)
        for target in ('publish','schedule','delete','captcha','login'):
            with self.assertRaises(PublishBlocked): r.select(self.nodes(),target,'fp')
        self.assertEqual(r.calls,0)
    def test_no_candidates_no_call(self):
        r=TypeSafeRouter(allowed=True,transport=self.response)
        with self.assertRaises(PublishBlocked): r.select([], 'profile','fp')
        self.assertEqual(r.calls,0)
    def test_unknown_price_and_over_budget(self):
        for cfg in ({'price_per_million':0},{'budget_usd':.00001},{'budget_usd':float('nan')}):
            r=TypeSafeRouter(allowed=True,transport=self.response,**cfg)
            with self.assertRaises(PublishBlocked): r.select(self.nodes(),'profile','fp')
            self.assertEqual(r.calls,0)
    def test_failed_call_reservation_and_no_retry(self):
        def fail(p): raise PublishBlocked('timeout')
        r=TypeSafeRouter(allowed=True,transport=fail)
        with self.assertRaises(PublishBlocked): r.select(self.nodes(),'profile','fp')
        self.assertEqual(r.calls,1); self.assertGreater(r.reserved,0)
    def test_low_confidence_malformed_and_nan_blocked(self):
        for value in (.4, float('nan'), True):
            answer={'type':'choice','choice':'x','probabilities':{'x':1.,'stop':0.},'confidence':value}
            with self.assertRaises(PublishBlocked): TypeSafeRouter.choice(answer,{'x','stop'})
    def test_unexpected_option_not_executed(self):
        with self.assertRaises(PublishBlocked):
            TypeSafeRouter.choice({'type':'choice','choice':'evil','probabilities':{'x':1},'confidence':1}, {'x','stop'})
    def test_model_version_change_stops(self):
        def other(p): r=self.response(p);r['model']='new-model';return r
        with self.assertRaises(PublishBlocked): TypeSafeRouter(allowed=True,transport=other).select(self.nodes(),'profile','fp')
    def test_disabled_does_not_call(self):
        with self.assertRaises(PublishBlocked): TypeSafeRouter(transport=self.response).select(self.nodes(),'profile','fp')
    def test_malformed_top_level_objects_blocked(self):
        for response in (None, [], {'answers':[]}, {'answers':{},'usage':[]}):
            with self.assertRaises(PublishBlocked):
                TypeSafeRouter(allowed=True,transport=lambda p:response).select(self.nodes(),'profile','fp')
    def test_distribution_must_be_mapping(self):
        for distribution in (None, [dict(x=1)], 'x'):
            with self.assertRaises(PublishBlocked):
                TypeSafeRouter.choice({'type':'choice','choice':'x','probabilities':distribution,'confidence':1},{'x'})


class SnapshotTests(unittest.TestCase):
    def fake(self):
        op=FastOperator.__new__(FastOperator)
        op._frame=None;op._frame_at=0;op._verified_account='account';op._account_at=time.monotonic()
        op.metrics={'ui_reads':0,'ui_seconds':0.,'cache_hits':0}
        xml='<hierarchy><node text="我" resource-id="profile" content-desc="" clickable="true" bounds="[0,0][10,10]"/></hierarchy>'
        op.shell=lambda *a,**kw:xml if a[0]=='cat' else ''
        return op
    def test_three_checks_share_one_frame(self):
        op=self.fake()
        for _ in range(3): op.find_nodes(resource_id='profile')
        self.assertEqual(op.metrics['ui_reads'],1);self.assertEqual(op.metrics['cache_hits'],2)
    def test_action_invalidation_and_expiry(self):
        op=self.fake();op.dump_ui();op.invalidate();op.dump_ui()
        self.assertEqual(op.metrics['ui_reads'],2)
        op._frame_at-=1;op.dump_ui();self.assertEqual(op.metrics['ui_reads'],3)
    def test_warning_blocked(self):
        with self.assertRaises(PublishBlocked): FastOperator.assert_safe([UiNode('请输入验证码','challenge','',True,(0,0,1,1))])
    def test_invalid_xml_never_reuses_last_frame(self):
        op=self.fake();op.dump_ui();op.invalidate();op.shell=lambda *a,**kw:'invalid'
        with self.assertRaises(PublishBlocked): op.dump_ui()
        self.assertIsNone(op._verified_account)
    def test_model_hint_invalidated_by_layout_change(self):
        a=self.fake().dump_ui();b=[UiNode('我','profile','',True,(1,1,11,11))]
        self.assertNotEqual(fingerprint(a),fingerprint(b))
    def test_real_shell_wrapper_invalidates_actions_and_account_on_restart(self):
        op=self.fake();op.dump_ui();del op.shell
        op.adb=lambda *a,**kw:''
        op.shell('input','tap','5','5')
        self.assertIsNone(op._frame);self.assertEqual(op._verified_account,'account')
        op.shell('am','force-stop','com.xingin.xhs')
        self.assertIsNone(op._verified_account)
    def test_grid_author_is_not_account_identity(self):
        card=UiNode('account','com.xingin.xhs:id/tv_nickname','',False,(0,0,10,10))
        with self.assertRaises(PublishBlocked):FastOperator.assert_account_header([card],'account')
        header=replace(card,resource_id='com.xingin.xhs:id/profile_new_page_avatar_card_nickname')
        FastOperator.assert_account_header([header,card],'account')
        with self.assertRaises(PublishBlocked):FastOperator.assert_account_header([header,header],'account')
        with self.assertRaises(PublishBlocked):FastOperator.assert_account_header([header],'wrong-account')
    def test_submit_uses_one_fresh_snapshot_bound_to_approved_content(self):
        op=self.fake();op.require_account_session=lambda c:None
        def node(text, rid):return UiNode(text,'com.xingin.xhs:id/'+rid,'',True,(0,0,10,10))
        nodes=[node(content().title,'editTitle'),node(content().body,'postNoteEditContentView'),node('发布','capaBigPostBtn')]
        with patch.object(op,'dump_ui',return_value=nodes) as read:
            self.assertEqual(op.submit_control(content()),nodes[-1])
            read.assert_called_once_with(force=True)
        with patch.object(op,'dump_ui',return_value=nodes):
            with self.assertRaises(PublishBlocked):op.submit_control(replace(content(),title='other title'))


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        (self.folder/'配图').mkdir()
        (self.folder/'body.txt').write_text('A test body #test')
        self.manifest={'id':'T001','status':'ready','title':'A test title','body_file':'body.txt',
                       'images':['01.png','02.png'],'tags':['test']}
        pages=[];origins=[]
        for i in (1,2):
            page={'page':f'{i:02d}'}
            for field in ('source','text_overlay','output'):
                name=f'{i:02d}.png' if field=='output' else f'{i:02d}-{field}.txt'
                data=(b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',1125,1500)+bytes([i])
                      if field=='output' else f'{i}-{field}'.encode())
                (self.folder/name).write_bytes(data)
                page[field]=name;page[field+'_sha256']=sha(data)
            pages.append(page);origins.append({'page':page['page'],'source':page['source'],'sha256':page['source_sha256']})
        self.mapping={'article_id':'T001','pages':pages}
        (self.folder/'配图/页面资产映射.json').write_text(json.dumps(self.mapping))
        (self.folder/'配图/生成记录.json').write_text(json.dumps({'article_id':'T001','pages':origins}))
        self.op=SimpleNamespace(config={'account_name':'test-account'},
            article=lambda aid:Article(self.folder,self.manifest),check_article=lambda *a,**kw:{'ok':True})
    def tearDown(self):self.temp.cleanup()
    def load(self):return Content.load(self.op,'T001')
    def test_content_digest_binds_copy_account_and_order(self):
        a=self.load();(self.folder/'body.txt').write_text('New copy #test')
        self.assertNotEqual(a.digest,self.load().digest)
        self.manifest['images'].reverse()
        with self.assertRaises(PublishBlocked):self.load()
    def test_changed_source_without_new_binding_blocked(self):
        (self.folder/'01-source.txt').write_text('changed')
        with self.assertRaises(PublishBlocked):self.load()
    def test_duplicate_pages_blocked(self):
        (self.folder/'02.png').write_bytes((self.folder/'01.png').read_bytes())
        with self.assertRaises(PublishBlocked):self.load()
    def test_path_escape_blocked(self):
        self.mapping['pages'][0]['source']='../outside.txt'
        (self.folder/'配图/页面资产映射.json').write_text(json.dumps(self.mapping))
        with self.assertRaises(PublishBlocked):self.load()
    def test_published_and_scheduled_blocked(self):
        for status in ('published','scheduled'):
            self.manifest['status']=status
            with self.assertRaises(PublishBlocked):self.load()


if __name__=='__main__': unittest.main()
