"""verify_password 算法自动识别回归：bcrypt 与 argon2id 双算法兼容。

背景：CI 侧 PASSWORD_HASH_ALGO 可在 bcrypt / argon2id 间切换，CD 登录必须
自动识别哈希前缀，不能硬编码 bcrypt（否则 CI 切到 argon2id 后 CD 登录抛
ValueError: Invalid salt → 500）。verify_password 对任何校验失败返回 False，
由调用方统一按「账号或密码错误」处理，绝不抛异常。

运行（项目根）:
    python -m pytest backend/tests/test_verify_password.py -q
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from backend.auth import verify_password

_PH = PasswordHasher()


class TestVerifyPassword(unittest.TestCase):
    def test_bcrypt_correct_password(self):
        hashed = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
        self.assertTrue(verify_password("secret", hashed))

    def test_bcrypt_wrong_password(self):
        hashed = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
        self.assertFalse(verify_password("wrong", hashed))

    def test_argon2id_correct_password(self):
        hashed = _PH.hash("secret")
        self.assertTrue(hashed.startswith("$argon2"))
        self.assertTrue(verify_password("secret", hashed))

    def test_argon2id_wrong_password(self):
        hashed = _PH.hash("secret")
        self.assertFalse(verify_password("wrong", hashed))

    def test_malformed_hash_returns_false_not_raise(self):
        # 既非 $argon2 也非合法 bcrypt（无 $2a/$2b 前缀）：bcrypt.checkpw 会抛
        # ValueError，verify_password 必须吞掉并返回 False，避免登录 500。
        self.assertFalse(verify_password("secret", "not-a-hash"))

    def test_argon2_mismatch_is_verification_error(self):
        # 锁定 argon2 走校验分支（而非误入 bcrypt）的哨兵：确保前缀分派正确。
        hashed = _PH.hash("secret")
        with self.assertRaises(VerifyMismatchError):
            _PH.verify(hashed, "wrong")


if __name__ == "__main__":
    unittest.main(verbosity=2)
