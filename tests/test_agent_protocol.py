import unittest
import tempfile
from pathlib import Path
from agent.journal import Journal
from agent.client import Client

class AgentSafetyTest(unittest.TestCase):
    def test_restart_cannot_repeat_submission(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'journal.db'; job={'id':'test-job','digest':'version-hash'}
            first=Journal(path); first.accept(job); first.submitting(job); first.db.close()
            after=Journal(path)
            with self.assertRaises(RuntimeError): after.accept(job)
            with self.assertRaises(RuntimeError): after.submitting(job)
            self.assertEqual(after.unresolved(),[('test-job','submitting')])
            after.db.close()

    def test_revision_cannot_change(self):
        with tempfile.TemporaryDirectory() as d:
            journal=Journal(Path(d)/'journal.db'); journal.accept({'id':'job','digest':'v1'})
            with self.assertRaises(RuntimeError): journal.submitting({'id':'job','digest':'v2'})
            journal.db.close()

    def test_https_required(self):
        with self.assertRaises(ValueError): Client('http://example.com','test')
        with self.assertRaises(ValueError): Client('http://192.168.1.1','test',True)
        Client('http://127.0.0.1:7131','test',True)

if __name__=='__main__': unittest.main()
