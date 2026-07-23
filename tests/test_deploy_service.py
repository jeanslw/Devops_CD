import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import settings
from app.database import Database
from app.services.deploy_service import DeployService


class DeployServiceTests(unittest.TestCase):
    def test_get_targets_handles_invalid_server_ids_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            with patch.object(settings, "db_driver", "sqlite"), patch.object(settings, "db_path", db_path):
                db = Database(db_path=db_path)
                svc = DeployService(db)

                targets = svc._get_targets("abc")

                self.assertEqual([], targets)


if __name__ == "__main__":
    unittest.main()
