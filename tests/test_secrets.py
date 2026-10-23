# oah.secrets 테스트 — 환경 변수 우선, 가짜 keychain·가짜 security 도구, Windows 자격 증명 왕복을 확인한다.
from __future__ import annotations

import os
import secrets as token_source
import sys
import unittest

from oah import secrets
from tests.helpers import make_tmp_dir, write_posix_tool


def fake_key() -> str:
    """A key-shaped value generated at runtime; never a real credential."""
    return "fake-" + token_source.token_hex(16)


class FakeKeychain:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.asked: list[str] = []

    def lookup(self, name: str) -> str | None:
        self.asked.append(name)
        return self.values.get(name)


class GetTest(unittest.TestCase):
    def test_env_wins_over_keychain(self) -> None:
        env_key, chain_key = fake_key(), fake_key()
        chain = FakeKeychain({"DEMO_KEY": chain_key})
        self.assertEqual(secrets.get("DEMO_KEY", env={"DEMO_KEY": env_key}, keychain=chain), env_key)
        self.assertEqual(chain.asked, [])

    def test_empty_env_falls_back(self) -> None:
        chain_key = fake_key()
        chain = FakeKeychain({"DEMO_KEY": chain_key})
        self.assertEqual(secrets.get("DEMO_KEY", env={"DEMO_KEY": ""}, keychain=chain), chain_key)
        self.assertEqual(chain.asked, ["DEMO_KEY"])

    def test_missing_everywhere_is_none(self) -> None:
        self.assertIsNone(secrets.get("DEMO_KEY", env={}, keychain=FakeKeychain({})))
        self.assertIsNone(secrets.get("DEMO_KEY", env={}, keychain=secrets.NoKeychain()))

    def test_empty_keychain_value_is_none(self) -> None:
        self.assertIsNone(secrets.get("DEMO_KEY", env={}, keychain=FakeKeychain({"DEMO_KEY": ""})))

    def test_invalid_name_raises_without_value(self) -> None:
        for name in ("", "-a", "has space", "a" * 65, "oah:x", "9START"):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                secrets.get(name, env={}, keychain=FakeKeychain({}))
            self.assertIn("OAH_SECRET_NAME_INVALID", str(caught.exception))

    def test_default_env_is_os_environ(self) -> None:
        value = fake_key()
        name = "OAH_TEST_SECRET_" + token_source.token_hex(4).upper()
        os.environ[name] = value
        self.addCleanup(os.environ.pop, name, None)
        self.assertEqual(secrets.get(name, keychain=FakeKeychain({})), value)

    def test_system_keychain_matches_platform(self) -> None:
        chain = secrets.system_keychain()
        if os.name == "nt":
            self.assertIsInstance(chain, secrets.WindowsCredentials)
        elif sys.platform == "darwin":
            self.assertIsInstance(chain, secrets.MacKeychain)
        else:
            self.assertIsInstance(chain, secrets.NoKeychain)


SECURITY_SCRIPT = r'''
if [ "$1" = "find-generic-password" ] && [ "$2" = "-s" ] && [ "$3" = "oah" ] && [ "$4" = "-a" ] && [ "$6" = "-w" ] && [ "$#" = "6" ]; then
  if [ -f "{store}/$5" ]; then cat "{store}/$5"; printf '\n'; exit 0; fi
  echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
  exit 44
fi
echo "unexpected arguments: $*" >&2
exit 2
'''


@unittest.skipIf(os.name == "nt", "the macOS keychain path runs a POSIX tool")
class MacKeychainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir(self)
        self.store = self.tmp / "store"
        self.store.mkdir()
        self.bin = self.tmp / "bin"
        write_posix_tool(self.bin, "security", SECURITY_SCRIPT.replace("{store}", str(self.store)))

    def test_mac_keychain_runs_security_tool(self) -> None:
        value = fake_key()
        (self.store / "DEMO_KEY").write_text(value, encoding="utf-8")
        chain = secrets.MacKeychain(search_path=str(self.bin))
        self.assertEqual(chain.lookup("DEMO_KEY"), value)
        self.assertEqual(secrets.get("DEMO_KEY", env={}, keychain=chain), value)

    def test_mac_keychain_not_found_is_none(self) -> None:
        self.assertIsNone(secrets.MacKeychain(search_path=str(self.bin)).lookup("DEMO_KEY"))

    def test_mac_keychain_missing_tool_is_none(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIsNone(secrets.MacKeychain(search_path=str(empty)).lookup("DEMO_KEY"))

    def test_mac_keychain_timeout_is_none(self) -> None:
        slow = self.tmp / "slow"
        write_posix_tool(slow, "security", "sleep 5")
        self.assertIsNone(secrets.MacKeychain(search_path=str(slow), timeout=0.3).lookup("DEMO_KEY"))

    def test_repr_never_shows_value(self) -> None:
        value = fake_key()
        (self.store / "DEMO_KEY").write_text(value, encoding="utf-8")
        chain = secrets.MacKeychain(search_path=str(self.bin))
        chain.lookup("DEMO_KEY")
        self.assertNotIn(value, repr(chain))


class DecodeBlobTest(unittest.TestCase):
    def test_non_ascii_utf16le_roundtrip(self) -> None:
        for suffix in ("secretŁ", "секрет", "키", "🔑"):
            for terminator in ("", "\x00"):
                with self.subTest(suffix=suffix, terminated=bool(terminator)):
                    value = fake_key() + suffix
                    self.assertEqual(secrets.decode_blob((value + terminator).encode("utf-16-le")), value)

    def test_decode_blob_utf16le(self) -> None:
        value = fake_key()
        self.assertEqual(secrets.decode_blob(value.encode("utf-16-le")), value)
        self.assertEqual(secrets.decode_blob((value + "\x00").encode("utf-16-le")), value)
        self.assertEqual(secrets.decode_blob("키-abc".encode("utf-16-le")), "키-abc")

    def test_decode_blob_rejects_empty_and_garbage(self) -> None:
        self.assertIsNone(secrets.decode_blob(b""))
        self.assertIsNone(secrets.decode_blob(b"\xff\xfe\xfd"))
        self.assertIsNone(secrets.decode_blob(b"\x00\xd8"))  # unpaired surrogate
        self.assertIsNone(secrets.decode_blob(b"\x00\x00"))
        self.assertIsNone(secrets.decode_blob(fake_key().encode("utf-8")))  # odd-length, not UTF-16-LE


@unittest.skipUnless(os.name == "nt", "Windows Credential Manager is Windows-only")
class WindowsCredentialsTest(unittest.TestCase):
    def _write(self, target: str, blob: bytes) -> None:
        import ctypes
        from ctypes import wintypes

        credential = secrets._CREDENTIALW()
        credential.Type = secrets.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.UserName = "oah-test"
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 1  # CRED_PERSIST_SESSION
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(secrets._CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        if not advapi32.CredWriteW(ctypes.byref(credential), 0):
            self.fail(f"CredWriteW failed with error {ctypes.get_last_error()}")
        self.addCleanup(advapi32.CredDeleteW, target, secrets.CRED_TYPE_GENERIC, 0)

    def test_windows_credential_roundtrip(self) -> None:
        name = "OAH_TEST_" + token_source.token_hex(6).upper()
        value = fake_key()
        self._write(f"oah:{name}", value.encode("utf-16-le"))
        self.assertEqual(secrets.WindowsCredentials().lookup(name), value)
        self.assertEqual(secrets.get(name, env={}), value)

    def test_windows_credential_invalid_utf16le_blob(self) -> None:
        name = "OAH_TEST_" + token_source.token_hex(6).upper()
        value = fake_key()
        self._write(f"oah:{name}", value.encode("utf-8"))
        self.assertIsNone(secrets.WindowsCredentials().lookup(name))

    def test_windows_non_ascii_utf16le_roundtrip(self) -> None:
        for suffix in ("secretŁ", "секрет", "키", "🔑"):
            with self.subTest(suffix=suffix):
                name = "OAH_TEST_" + token_source.token_hex(6).upper()
                value = fake_key() + suffix
                self._write(f"oah:{name}", value.encode("utf-16-le"))
                self.assertEqual(secrets.WindowsCredentials().lookup(name), value)
                self.assertEqual(secrets.get(name, env={}), value)

    def test_windows_missing_credential_is_none(self) -> None:
        name = "OAH_TEST_" + token_source.token_hex(6).upper()
        self.assertIsNone(secrets.WindowsCredentials().lookup(name))


if __name__ == "__main__":
    unittest.main()
