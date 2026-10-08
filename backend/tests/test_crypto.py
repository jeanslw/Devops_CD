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
            with (
                patch.object(settings, "secret_key", "change_me_to_secret"),
                patch.object(crypto, "_key_file_path", return_value=key_file),
            ):
                derived = crypto._get_secret_key()
            # 必须落盘了随机密钥文件
            self.assertTrue(key_file.exists())
            # 派生结果不得等于该弱值直接派生的密钥
            self.assertNotEqual(derived, crypto._derive_key("change_me_to_secret"))
            # 再次读取应稳定复用文件中的密钥
            with (
                patch.object(settings, "secret_key", ""),
                patch.object(crypto, "_key_file_path", return_value=key_file),
            ):
                self.assertEqual(crypto._get_secret_key(), derived)

    def test_explicit_strong_key_still_used_directly(self):
        strong = "a-very-long-random-key-value-not-in-any-template-123456"
        with patch.object(settings, "secret_key", strong):
            self.assertEqual(crypto._get_secret_key(), crypto._derive_key(strong))


class TestPerDeploySaltAndBackwardCompat(unittest.TestCase):
    """按部署随机盐：持久化稳定 + 旧数据兼容解密 + 解密失败分级告警。"""

    def test_roundtrip_with_new_salt(self):
        secret = "s3cr3t-p@ssw0rd-123"
        enc = crypto.encrypt(secret)
        self.assertTrue(enc.startswith(crypto.ENCRYPT_PREFIX))
        self.assertEqual(crypto.decrypt(enc), secret)

    def test_salt_is_persisted_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            salt_file = Path(tmp) / ".cd_secret_key.salt"
            with patch.object(crypto, "_salt_file_path", return_value=salt_file):
                s1 = crypto._get_or_create_salt()
                s2 = crypto._get_or_create_salt()
            self.assertEqual(s1, s2)  # 二次读取稳定复用
            self.assertTrue(salt_file.exists())

    def test_random_salt_differs_from_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            salt_file = Path(tmp) / ".cd_secret_key.salt"
            with patch.object(crypto, "_salt_file_path", return_value=salt_file):
                new_salt = crypto._get_or_create_salt()
        self.assertNotEqual(new_salt, crypto._LEGACY_SALT)

    def test_legacy_ciphertext_still_decrypts(self):
        # 升级前用历史固定盐派生的兼容密钥加密的数据，升级后应仍可解密
        token = crypto._legacy_fernet.encrypt(b"legacy-secret-value")
        self.assertEqual(crypto.decrypt(crypto.ENCRYPT_PREFIX + token.decode()), "legacy-secret-value")

    def test_malformed_ciphertext_returns_empty(self):
        # 非法 base64 → 分级 ERROR 日志 + 空值降级（不抛异常）
        self.assertEqual(crypto.decrypt("enc:!!!not-base64!!!"), "")

    def test_plaintext_passthrough_unchanged(self):
        # 无 enc: 前缀的历史明文原样返回
        self.assertEqual(crypto.decrypt("plain-password"), "plain-password")


if __name__ == "__main__":
    unittest.main(verbosity=2)
