"""One device session, action-invalidated snapshots, ordinary-navigation hints."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from scripts.xhs_operator import XhsOperator, UiNode, PACKAGE, INPUT_HELPER_PACKAGE, OperatorError
from .publish_state import PublishBlocked

PREFIX = PACKAGE+':id/'


def fingerprint(nodes):
    return hashlib.sha256(json.dumps([(n.text, n.resource_id, n.content_desc, n.bounds, n.clickable)
                                     for n in nodes], ensure_ascii=False).encode()).hexdigest()


class FastOperator(XhsOperator):
    def __init__(self, config_path, router=None):
        super().__init__(Path(config_path))
        self.router = router
        self._frame = None
        self._frame_at = 0
        self._hardware = None
        self._account_at = 0
        self._verified_account = None
        self.metrics = {'adb_commands': 0, 'ui_reads': 0, 'ui_seconds': 0., 'cache_hits': 0, 'wake_calls': 0}

    def adb(self, *args, check=True):
        if self._device_serial is None:
            self.require_device()
        self.metrics['adb_commands'] += 1
        try:
            result = subprocess.run([self.adb_path, '-s', self._device_serial, *args],
                                    capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired:
            self.invalidate(account=True)
            raise PublishBlocked('ADB command timed out; no automatic retry') from None
        if check and result.returncode:
            self.invalidate(account=True)
            raise PublishBlocked('ADB command failed')
        return result.stdout.strip()

    def invalidate(self, account=False):
        self._frame = None
        if account:
            self._verified_account = None
            self._account_at = 0

    def shell(self, *args, check=True):
        if args and args[0] in {'input', 'am', 'monkey', 'wm', 'svc'}:
            self.invalidate(account=args[0] in {'monkey'} or args[:2] == ('am', 'force-stop'))
        return super().shell(*args, check=check)

    @staticmethod
    def assert_safe(nodes):
        # Use app controls, not text of published articles, as interruption cues.
        markers = ('验证码', '账号异常', '发布失败', '内容违规', '登录后', '手机号登录', '账号违规')
        for n in nodes:
            if n.resource_id in {PREFIX+'imageNoteTextView', PREFIX+'postNoteEditContentView', PREFIX+'noteTitleTV'}:
                continue
            if any(w in n.text for w in markers):
                raise PublishBlocked('Login, challenge or platform warning; manual review required')

    def dump_ui(self, force=False):
        if not force and self._frame is not None and time.monotonic()-self._frame_at < .8:
            self.metrics['cache_hits'] += 1
            return list(self._frame)
        started = time.monotonic()
        # A unique path prevents a failed UI dump from reading an older frame.
        remote = '/sdcard/xhs_fast_'+uuid.uuid4().hex+'.xml'
        try:
            self.shell('uiautomator', 'dump', '--compressed', remote)
            xml = self.shell('cat', remote)
            root = ET.fromstring(xml)
        except (ET.ParseError, OperatorError):
            self.invalidate(account=True)
            raise PublishBlocked('No fresh valid UI snapshot') from None
        finally:
            self.shell('rm', '-f', remote, check=False)
        nodes = []
        for node in root.iter('node'):
            bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
            if bounds:
                nodes.append(UiNode(node.get('text', ''), node.get('resource-id', ''),
                                    node.get('content-desc', ''), node.get('clickable') == 'true',
                                    tuple(map(int, bounds.groups()))))
        try:
            self.assert_safe(nodes)
        except PublishBlocked:
            self.invalidate(account=True)
            raise
        self.metrics['ui_reads'] += 1
        self.metrics['ui_seconds'] += time.monotonic()-started
        self._frame, self._frame_at = tuple(nodes), time.monotonic()
        return nodes

    def try_ui_tars_recovery(self, **kwargs):
        # No unbounded vision fallback in the fast path. Missing controls stop.
        return False

    def helper_installed(self):
        if self._hardware:
            return self._hardware['input_helper_installed']
        return super().helper_installed()

    def hardware(self):
        if self._hardware:
            return self._hardware
        self.require_device()
        model = self.shell('getprop', 'ro.product.model')
        size = self.shell('wm', 'size')
        packages = self.shell('pm', 'list', 'packages')
        self._hardware = {'model_supported': model == self.config['expected_device_model'],
            'screen_supported': self.config.get('expected_screen_size', '1080x2280') in size,
            'xiaohongshu_installed': 'package:'+PACKAGE in packages.splitlines(),
            'input_helper_installed': 'package:'+INPUT_HELPER_PACKAGE in packages.splitlines()}
        if not all(self._hardware.values()):
            raise PublishBlocked('Device/app/helper preflight failed')
        return self._hardware

    def begin(self):
        self.hardware()
        self.metrics['wake_calls'] += 1
        result = super().begin()
        if not result['ok']:
            raise PublishBlocked('Wake/unlock/app launch did not complete')
        return result

    def wait_frame(self, predicate, timeout=8):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            nodes = self.dump_ui(force=True)
            if predicate(nodes):
                return nodes
            time.sleep(.2)
        raise PublishBlocked('Expected UI transition not observed')

    def ordinary(self, rid, target, wait=.25):
        nodes = self.dump_ui()
        found = [n for n in nodes if n.resource_id == PREFIX+rid]
        if len(found) == 1:
            node = found[0]
        else:
            if not self.router:
                raise PublishBlocked('Ordinary control missing')
            before = fingerprint(nodes)
            index = self.router.select(nodes, target, before)
            fresh = self.dump_ui(force=True)
            if fingerprint(fresh) != before:
                raise PublishBlocked('UI changed during TypeSafe call')
            node = fresh[index]
        self.shell('input', 'tap', *map(str, node.center))
        time.sleep(wait)

    def account(self):
        nodes = self.dump_ui(force=True)
        if any(n.resource_id == PREFIX+'noteTitleTV' for n in nodes):
            self.shell('input', 'keyevent', 'KEYCODE_BACK')
            nodes = self.wait_frame(lambda ns: any(n.resource_id == PREFIX+'index_me' for n in ns))
        if any(n.resource_id == PREFIX+'searchCancelTv' for n in nodes):
            self.tap_resource(PREFIX+'searchCancelTv', wait=.25)
        self.ordinary('index_me', 'profile')
        nodes = self.wait_frame(lambda ns: any(n.resource_id == PREFIX+'profileSearchEntrance' for n in ns))
        header = PREFIX+'profile_new_page_avatar_card_nickname'
        if not any(n.resource_id == header for n in nodes):
            # Profile retains scroll position. Grid `tv_nickname` fields are
            # authors of cards, NOT identity evidence for the signed-in account.
            for _ in range(3):
                self.shell('input', 'swipe', '540', '450', '540', '1850', '300')
            nodes = self.dump_ui(force=True)
        self.assert_account_header(nodes, self.config['account_name'])
        self._verified_account, self._account_at = self.config['account_name'], time.monotonic()
        return self._verified_account

    @staticmethod
    def assert_account_header(nodes, expected):
        nicknames = [n.text for n in nodes if n.resource_id == PREFIX+'profile_new_page_avatar_card_nickname']
        if nicknames != [expected]:
            raise PublishBlocked('Account identity header missing, ambiguous or mismatched')

    def require_account_session(self, content):
        if self._verified_account != content.account or time.monotonic()-self._account_at > 300:
            raise PublishBlocked('Account session expired or changed')
        if PACKAGE not in self.current_focus():
            self.invalidate(account=True)
            raise PublishBlocked('App left foreground')

    def check_duplicates(self, content):
        # Local ledger runs first. Historical imports still require an exact
        # current/previous-title search; broad keywords are intentionally absent.
        self.tap_resource(PREFIX+'profileSearchEntrance', wait=.25)
        for title in dict.fromkeys([content.title, content.prior_title]):
            if not title:
                continue
            self.paste_into(PREFIX+'searchViewEt', title)
            self.shell('input', 'keyevent', 'KEYCODE_ENTER')
            def finished(ns):
                return any(n.text == '没找到我的笔记' for n in ns)
            # Missing/ambiguous or related results do not count as proof of absence.
            self.wait_frame(finished)
        self.tap_resource(PREFIX+'searchCancelTv', wait=.25)

    def compose_direct(self, content):
        sync = self.sync_article(content.article, allowed_statuses=('ready', 'designing', 'writing', 'draft'))
        if [r['sha256'] for r in sync['image_hashes']] != list(content.hashes):
            raise PublishBlocked('Content changed during transfer')
        self.tap_resource(PREFIX+'index_post', wait=.3)
        self.wait_for_text('从相册选择')
        self.tap_node(text='从相册选择', wait=.5)
        self.wait_for_resource(PREFIX+'albumPopLayout')
        labels = self.find_nodes(resource_id=PREFIX+'albumNameTv')
        self.tap_resource(PREFIX+('albumNameTv' if labels else 'albumPopLayout'), wait=.3)
        album = self.find_album_name(sync['album_name'])
        self.shell('input', 'tap', *map(str, album.center))
        self.wait_frame(lambda ns: sum(n.resource_id == PREFIX+'selectableLayout' for n in ns) == len(content.images))
        choices = sorted(self.find_nodes(resource_id=PREFIX+'selectableLayout'), key=lambda n:(n.bounds[1], n.bounds[0]))
        expected = [f"{sync['album_name']}-{i:02d}.png" for i in range(1, len(content.images)+1)]
        if len(choices) != len(content.images) or sync['media_store_order'] != expected:
            raise PublishBlocked('Album order/count mismatch')
        for node in choices:
            self.shell('input', 'tap', *map(str, node.center))
        nxt = self.wait_for_resource(PREFIX+'bottomGoNext')
        if nxt.text != f'下一步({len(content.images)})':
            raise PublishBlocked('Selected count mismatch')
        order_proof = self.screenshot('fast-'+content.article+'-'+content.digest[:12]+'-order.png')
        self.shell('input', 'tap', *map(str, nxt.center))
        self.wait_for_resource(PREFIX+'capa_light_edit_next')
        self.ordinary('capa_light_edit_next', 'next')
        if self.find_nodes(resource_id=PREFIX+'text_refuse'):
            self.tap_resource(PREFIX+'text_refuse', wait=.25)
        self.wait_for_resource(PREFIX+'editTitle')
        fill = self.fill_article(content.article, allowed_statuses=('ready', 'designing', 'writing', 'draft'))
        if not fill['ok']:
            raise PublishBlocked('Title/body/native topic mismatch')
        self.shell('input', 'keyevent', 'KEYCODE_BACK')
        return {'image_hashes': sync['image_hashes'], 'cover_is_first': sync['media_store_order'][0] == expected[0],
                'topics_verified': fill['topics_verified'], 'image_count': len(content.images),
                'order_evidence': str(order_proof),
                'order_evidence_sha256': hashlib.sha256(order_proof.read_bytes()).hexdigest()}

    def submit_control(self, content):
        self.require_account_session(content)
        nodes = self.dump_ui(force=True)
        titles = [n.text for n in nodes if n.resource_id == PREFIX+'editTitle']
        bodies = [n.text for n in nodes if n.resource_id == PREFIX+'postNoteEditContentView']
        count = sum(n.resource_id == PREFIX+'capaItemImage' for n in nodes)
        if (titles != [content.title] or len(bodies) != 1 or
            self.normalize_editor_text(bodies[0]) != self.normalize_editor_text(content.body) or
            count != len(content.images)):
            raise PublishBlocked('Compose changed before submission')
        buttons = [n for n in nodes if n.resource_id == PREFIX+'capaBigPostBtn']
        if not buttons:
            buttons = [n for n in nodes if n.resource_id == PREFIX+'capaTopPostBtn']
        if len(buttons) != 1:
            raise PublishBlocked('Publish control missing/ambiguous')
        return buttons[0]

    def commit_once(self, button):
        # Only called after the durable ledger has committed `submitting`.
        self.shell('input', 'tap', *map(str, button.center))

    def accept_public(self, content):
        nodes = self.wait_frame(lambda ns: any(n.resource_id == PREFIX+'index_post' for n in ns)
                               and not any(n.resource_id == PREFIX+'editTitle' for n in ns), timeout=75)
        if not any(n.resource_id == PREFIX+'profileSearchEntrance' for n in nodes):
            self.ordinary('index_me', 'profile')
        card = self.find_profile_post_card(content.title)
        self.shell('input', 'tap', *map(str, card.center))
        def accepted(ns):
            fields = {n.resource_id: n.text for n in ns}
            return (fields.get(PREFIX+'noteTitleTV') == content.title and
                    self.normalize_editor_text(fields.get(PREFIX+'imageNoteTextView', '')) == self.normalize_editor_text(content.body) and
                    fields.get(PREFIX+'notePrivacyTv') == '公开可见')
        self.wait_frame(accepted, timeout=15)
        screenshot = self.screenshot('fast-'+content.article+'-'+content.digest[:12]+'-public.png')
        return {'privacy': '公开可见', 'title_matches': True, 'body_matches': True,
                'profile_card_verified': True, 'evidence': str(screenshot), 'evidence_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()}

    def cleanup_verified(self):
        self.end()
        def is_off():
            states = re.findall(r'^\s*mScreenState=(\w+)\s*$', self.shell('dumpsys', 'display'), re.M)
            power = self.shell('dumpsys', 'power')
            return bool(states) and all(s == 'OFF' for s in states) and any(s in power for s in ('mWakefulness=Asleep', 'mWakefulness=Dozing'))
        first = is_off()
        time.sleep(.3)
        result = first and is_off()
        self.invalidate(account=True)
        return result
