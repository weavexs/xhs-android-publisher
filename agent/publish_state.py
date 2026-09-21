"""Immutable content identity and crash-safe, account/article-level submit fence."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import time


class PublishBlocked(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Content:
    account: str
    article: str
    title: str
    body: str
    tags: tuple
    images: tuple
    hashes: tuple
    digest: str
    prior_title: str = ''

    @classmethod
    def load(cls, operator, article_id):
        a = operator.article(article_id)
        m = a.manifest
        if m.get('status') in {'published', 'scheduled'} or m.get('published_at'):
            raise PublishBlocked('Already published/scheduled; reconcile instead of resubmitting')
        if m.get('status') not in {'ready', 'designing', 'writing', 'draft'}:
            raise PublishBlocked('Unsupported content state')
        check = operator.check_article(article_id, allowed_statuses=('ready', 'designing', 'writing', 'draft'))
        if not check['ok']:
            raise PublishBlocked('Content validation failed')
        images = tuple(p.resolve() for p in a.image_paths)
        if any(not p.is_relative_to(a.folder.resolve()) for p in images):
            raise PublishBlocked('Image outside article directory')
        hashes = []
        for p in images:
            data = p.read_bytes()
            if data[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', data[16:24]) != (1125, 1500):
                raise PublishBlocked('Expected 1125x1500 PNG')
            hashes.append(sha(data))
        if len(set(hashes)) != len(hashes):
            raise PublishBlocked('Duplicate page images')
        # Validate immutable delivery bindings locally; never regenerate/replace
        # art during publication. Production approval remains a separate gate.
        mapping = json.loads((a.folder/'配图/页面资产映射.json').read_text())
        provenance = json.loads((a.folder/'配图/生成记录.json').read_text())
        if (mapping.get('article_id') != article_id or provenance.get('article_id') != article_id or
            len(mapping.get('pages', [])) != len(images) or len(provenance.get('pages', [])) != len(images)):
            raise PublishBlocked('Missing page delivery/provenance bindings')
        for index, (page, origin, output) in enumerate(zip(mapping['pages'], provenance['pages'], images), 1):
            if page.get('page') != f'{index:02d}' or origin.get('page') != page['page']:
                raise PublishBlocked('Page index binding mismatch')
            for field in ('source', 'text_overlay', 'output'):
                path = (a.folder/page[field]).resolve()
                if not path.is_relative_to(a.folder.resolve()) or sha(path.read_bytes()) != page[field+'_sha256']:
                    raise PublishBlocked('Page asset SHA mismatch')
            if ((a.folder/page['output']).resolve() != output or origin.get('sha256') != page['source_sha256'] or
                origin.get('source') != page['source']):
                raise PublishBlocked('Image/provenance order mismatch')
        tags = tuple(str(t).lstrip('#').strip() for t in m.get('tags', []))
        if not tags or any(not t or '#'+t not in a.body for t in tags):
            raise PublishBlocked('Body/topic mismatch')
        identity = {'account': operator.config['account_name'], 'article': article_id,
                    'title': m['title'], 'body': a.body, 'tags': tags, 'images': hashes}
        digest = sha(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode())
        return cls(identity['account'], article_id, m['title'], a.body, tags, images,
                   tuple(hashes), digest, m.get('editorial_revision', {}).get('previous_title', ''))


class PublishLedger:
    """A new task ID or changed revision cannot bypass an unresolved article."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=3)
        os.chmod(path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS publications (
            account TEXT, article TEXT, digest TEXT, task TEXT UNIQUE, state TEXT,
            evidence TEXT DEFAULT '{}', updated REAL, PRIMARY KEY(account,article))''')
        self.db.commit()

    def reserve(self, content, task):
        if not task or len(task) > 128:
            raise PublishBlocked('Explicit bounded task ID required')
        try:
            with self.db:
                self.db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
                                (content.account, content.article, content.digest, task, 'prepared', '{}', time.time()))
        except sqlite3.IntegrityError:
            raise PublishBlocked('Known task/article: inspect existing result; never blind retry') from None

    def transition(self, content, task, expected, target, evidence=None):
        with self.db:
            cur = self.db.execute('''UPDATE publications SET state=?,evidence=?,updated=?
                WHERE account=? AND article=? AND digest=? AND task=? AND state=?''',
                (target, json.dumps(evidence or {}, ensure_ascii=False), time.time(),
                 content.account, content.article, content.digest, task, expected))
            if cur.rowcount != 1:
                raise PublishBlocked('Invalid or repeated submission boundary')

    def close(self):
        self.db.close()


@contextmanager
def device_lock(serial, root=None):
    # Global per-user lock, not one lock per arbitrary config/state directory.
    root = Path(root or Path.home()/'.local/share/xhs-phone-agent/locks')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root/(sha(serial.encode())+'.lock')).open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishBlocked('Device is busy') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
