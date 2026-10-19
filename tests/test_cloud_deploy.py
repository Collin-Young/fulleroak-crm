import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vumber_client import VumberError, VumberSessionStore


class CloudDeploymentTests(unittest.TestCase):
    def test_host_key_encrypts_portable_vumber_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vumber_session.bin"
            session = {"access_token": "access", "account_no": "123"}
            with patch.dict(os.environ, {"CRM_DATA_ENCRYPTION_KEY": "render-secret"}):
                store = VumberSessionStore(path)
                store.save(session)
                self.assertEqual(store.load(), session)
                self.assertNotIn(b"access", path.read_bytes())

            with patch.dict(os.environ, {"CRM_DATA_ENCRYPTION_KEY": "wrong-secret"}):
                with self.assertRaises(VumberError):
                    VumberSessionStore(path).load()


if __name__ == "__main__":
    unittest.main()
