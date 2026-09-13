"""crypto 弱默认密钥回归：公开示例值必须被忽略，走文件随机密钥路径。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import crypto
from backend.config import settings


class TestInsecureDefaultKeys(unittest.TestCase):
    def test_known_public_values_blocked(self):
        # 两个公开模板里出现过的值都必须拉黑
        self.assertIn("devops_cd_2026", crypto._INSECURE_DEFAULT_KEYS)
        self.assertIn("change_me_to_secret", crypto._INSECURE_DEFAULT_KEYS)

    def test_insecure_value_falls_back_to_generated_file_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / ".cd_secret_key"
            with patch.object(settings, "secret_key", "change_me_to_secret"), \
                 patch.object(crypto, "_key_file_path", return_value=key_file):
                derived = crypto._get_secret_key()
            # 必须落盘了随机密钥文件
            self.assertTrue(key_file.exists())
            # 派生结果不得等于该弱值直接派生的密钥
            self.assertNotEqual(derived, crypto._derive_key("change_me_to_secret"))
            # 再次读取应稳定复用文件中的密钥
            with patch.object(settings, "secret_key", ""), \
                 patch.object(crypto, "_key_file_path", return_value=key_file):
                self.assertEqual(crypto._get_secret_key(), derived)

    def test_explicit_strong_key_still_used_directly(self):
        strong = "a-very-long-random-key-value-not-in-any-template-123456"
        with patch.object(settings, "secret_key", strong):
            self.assertEqual(crypto._get_secret_key(), crypto._derive_key(strong))


if __name__ == "__main__":
    unittest.main(verbosity=2)
