"""Durable local boundary. Once claimed, a job is never automatically replayed."""
import sqlite3
import os
from pathlib import Path

class Journal:
    def __init__(self,path):
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.db=sqlite3.connect(path)
        os.chmod(path,0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,state TEXT NOT NULL,digest TEXT NOT NULL)')
        self.db.commit()

    def accept(self,job):
        try:
            with self.db: self.db.execute('INSERT INTO executions VALUES(?,?,?)',(job['id'],'claimed',job['digest']))
        except sqlite3.IntegrityError:
            raise RuntimeError('Previously seen job: manual reconciliation required') from None

    def submitting(self,job):
        with self.db:
            cur=self.db.execute("UPDATE executions SET state='submitting' WHERE id=? AND state='claimed' AND digest=?",(job['id'],job['digest']))
            if cur.rowcount!=1: raise RuntimeError('Submission boundary already crossed or version changed')

    def unresolved(self):
        return self.db.execute("SELECT id,state FROM executions WHERE state!='verified'").fetchall()

    def verified(self,job):
        with self.db:
            cur=self.db.execute("UPDATE executions SET state='verified' WHERE id=? AND state='submitting' AND digest=?",(job['id'],job['digest']))
            if cur.rowcount!=1: raise RuntimeError('Missing submission boundary')
