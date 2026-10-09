"""crypto 回归：弱默认密钥被忽略 + 派生盐确定性 + 历史盐（固定盐 / v1.5.7 落盘盐）兼容解密。"""

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import crypto
from backend.config import BASE_DIR, settings


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

    def test_unwritable_key_file_raises_runtime_error(self):
        # 密钥文件写入失败（容器只读 / 目录不存在）必须抛带修复指引的 RuntimeError，
        # 而不是裸 OSError 栈
        with tempfile.TemporaryDirectory() as tmp:
            unwritable = Path(tmp) / "no-such-dir" / ".cd_secret_key"
            with (
                patch.object(settings, "secret_key", ""),
                patch.object(crypto, "_key_file_path", return_value=unwritable),
            ):
                with self.assertRaises(RuntimeError):
                    crypto._get_secret_key()


class TestDerivedSaltAndBackwardCompat(unittest.TestCase):
    """盐从 SECRET_KEY 派生：确定性 + 按 secret 唯一 + 旧数据兼容解密 + 解密失败分级告警。"""

    def test_roundtrip_with_new_key(self):
        secret = "s3cr3t-p@ssw0rd-123"
        enc = crypto.encrypt(secret)
        self.assertTrue(enc.startswith(crypto.ENCRYPT_PREFIX))
        self.assertEqual(crypto.decrypt(enc), secret)

    def test_derivation_is_deterministic_for_same_secret(self):
        # 同一 secret 两次派生结果一致：盐从 SECRET_KEY 派生，无需落盘即跨重启稳定
        self.assertEqual(crypto._derive_salt("k1"), crypto._derive_salt("k1"))
        self.assertEqual(crypto._derive_key("k1"), crypto._derive_key("k1"))

    def test_different_secrets_derive_different_keys(self):
        # 不同 secret 派生密钥不同：盐随 secret 而异，按部署天然唯一
        self.assertNotEqual(crypto._derive_salt("k1"), crypto._derive_salt("k2"))
        self.assertNotEqual(crypto._derive_key("k1"), crypto._derive_key("k2"))

    def test_derived_salt_differs_from_legacy(self):
        self.assertNotEqual(crypto._derive_salt(crypto._secret_raw), crypto._LEGACY_SALT)

    def test_legacy_fixed_salt_ciphertext_still_decrypts(self):
        # 升级前用历史固定盐派生的密钥加密的数据，升级后经 decrypt() 两级回退仍可解出
        secret = "legacy-secret-value"
        legacy_fernet = Fernet(crypto._derive_key(crypto._secret_raw, crypto._LEGACY_SALT))
        token = legacy_fernet.encrypt(secret.encode("utf-8"))
        self.assertEqual(crypto.decrypt(crypto.ENCRYPT_PREFIX + token.decode()), secret)

    def test_malformed_ciphertext_returns_empty(self):
        # 非法 base64 → 分级 ERROR 日志 + 空值降级（不抛异常）
        self.assertEqual(crypto.decrypt("enc:!!!not-base64!!!"), "")

    def test_plaintext_passthrough_unchanged(self):
        # 无 enc: 前缀的历史明文原样返回
        self.assertEqual(crypto.decrypt("plain-password"), "plain-password")


class TestV157StoredSaltBackwardCompat(unittest.TestCase):
    """v1.5.7 曾落盘的随机盐（.cd_secret_key.salt）：只读回退，保证当时写入的密文仍可解出。

    候选位置只认 CD 项目根目录（密钥文件同目录）；DB_PATH 目录不读、不写。
    """

    def _patch_isolated(self, tmp, db_path: str = ""):
        """隔离密钥路径，并把 DB_PATH 换成一个假值。

        db_path 只用于证明「DB_PATH 不参与任何落盘路径推导」：即便它指向别的项目目录
        （历史 .env 里就是 ../Devops_Glue/config/data/data.db），也不得被读取到、更不得被写入。
        """
        return patch.multiple(
            crypto,
            _key_file_path=lambda: Path(tmp) / ".cd_secret_key",
            settings=SimpleNamespace(db_path=db_path),
        )

    def test_read_salt_when_present(self):
        raw_salt = b"0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            salt_file = Path(tmp) / ".cd_secret_key.salt"
            salt_file.write_text(base64.b64encode(raw_salt).decode("ascii"), encoding="utf-8")
            self.assertEqual(crypto._read_salt(salt_file), raw_salt)

    def test_read_salt_none_when_absent_empty_or_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            salt_file = Path(tmp) / ".cd_secret_key.salt"
            self.assertIsNone(crypto._read_salt(salt_file))  # 文件不存在（read_only 容器即此路径）
            for content in ("", "   ", "!!!not-base64!!!"):
                salt_file.write_text(content, encoding="utf-8")
                self.assertIsNone(crypto._read_salt(salt_file))

    def test_stored_salts_from_key_file_dir(self):
        raw_salt = b"aaaabbbbccccdddd"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".cd_secret_key.salt").write_text(base64.b64encode(raw_salt).decode("ascii"), encoding="utf-8")
            with self._patch_isolated(tmp):
                self.assertEqual(crypto._stored_salts(), [raw_salt])

    def test_db_path_dir_salt_is_ignored(self):
        # 反例（回归）：盐文件只存在于 DB_PATH 目录时必须**读不到**。
        # DB_PATH 可能指向 Devops-Glue 等第三方项目目录（.env 里就是 ../Devops_Glue/config/data/data.db），
        # CD 不读取也不写入别的项目的文件 —— v1.5.7 曾把 .cd_secret_key.salt 写进 Glue 的 config/data。
        raw_salt = b"eeeeffff00001111"
        with tempfile.TemporaryDirectory() as tmp:
            key_dir = Path(tmp) / "cd-root"
            db_dir = Path(tmp) / "glue-config-data"
            key_dir.mkdir()
            db_dir.mkdir()
            (db_dir / ".cd_secret_key.salt").write_text(base64.b64encode(raw_salt).decode("ascii"), encoding="utf-8")
            with patch.multiple(
                crypto,
                _key_file_path=lambda: key_dir / ".cd_secret_key",
                settings=SimpleNamespace(db_path=str(db_dir / "data.db")),
            ):
                self.assertEqual(crypto._stored_salts(), [])
                self.assertEqual(crypto._salt_file_paths(), [key_dir / ".cd_secret_key.salt"])

    def test_stored_salts_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._patch_isolated(tmp, db_path=str(Path(tmp) / "data.db")):
                self.assertEqual(crypto._stored_salts(), [])

    def test_v157_salt_file_ciphertext_still_decrypts(self):
        # v1.5.7 用落盘随机盐加密的凭据：本版 decrypt() 经回退链仍能解出（否则会静默变空值）
        raw_salt = b"fedcba9876543210"
        salt_fernet = Fernet(crypto._derive_key(crypto._secret_raw, raw_salt))
        token = crypto.ENCRYPT_PREFIX + salt_fernet.encrypt(b"v157-stored-salt-secret").decode("utf-8")
        with patch.object(crypto, "_salt_file_fernets", [salt_fernet]):
            self.assertEqual(crypto.decrypt(token), "v157-stored-salt-secret")

    def test_no_stored_salt_keeps_plain_path_working(self):
        # 没有盐文件时（read_only 容器 / 已清理）：回退链仍然工作，且不抛异常
        with tempfile.TemporaryDirectory() as tmp:
            with self._patch_isolated(tmp):
                self.assertEqual(crypto._stored_salts(), [])
        with patch.object(crypto, "_salt_file_fernets", []):
            self.assertEqual(crypto.decrypt(crypto.encrypt("still-works")), "still-works")
            legacy = Fernet(crypto._derive_key(crypto._secret_raw, crypto._LEGACY_SALT))
            token = crypto.ENCRYPT_PREFIX + legacy.encrypt(b"v156-value").decode("utf-8")
            self.assertEqual(crypto.decrypt(token), "v156-value")


class TestRuntimeFilesStayInCdDir(unittest.TestCase):
    """落盘位置不变量：CD 只在**自身项目目录**内读写运行时文件。

    DB_PATH 指向的是与 Devops-Glue 共享的数据库（历史 .env：../Devops_Glue/config/data/data.db），
    绝不允许由它推导密钥 / 盐文件位置 —— 否则 CD 会把 .cd_secret_key / .cd_secret_key.salt
    写进 Glue 的目录（v1.5.7 sqlite 模式即如此，已在 v1.5.7 后续版本修正）。
    """

    _GLUEISH_DB = str(Path("..") / "Devops_Glue" / "config" / "data" / "data.db")

    def test_key_file_path_is_cd_root_even_for_sqlite(self):
        with patch.multiple(
            crypto, settings=SimpleNamespace(db_driver="sqlite", db_path=self._GLUEISH_DB)
        ):
            self.assertEqual(crypto._key_file_path(), BASE_DIR / ".cd_secret_key")

    def test_salt_paths_are_cd_root_only(self):
        with patch.multiple(
            crypto, settings=SimpleNamespace(db_driver="sqlite", db_path=self._GLUEISH_DB)
        ):
            paths = crypto._salt_file_paths()
        self.assertEqual(paths, [BASE_DIR / ".cd_secret_key.salt"])
        for path in paths:
            self.assertTrue(str(path).startswith(str(BASE_DIR)), f"{path} 落在 CD 项目目录之外")


if __name__ == "__main__":
    unittest.main(verbosity=2)
