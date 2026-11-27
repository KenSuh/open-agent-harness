# oah.secrets 저장 경로 테스트 — 기기 키 저장소의 상태 구분, 표준 입력으로만 건네는 macOS 저장, Windows 자격 증명 쓰기, 파이프 인계의 상한을 확인한다.
from __future__ import annotations

import io
import os
import secrets as token_source
import sys
import unittest
from unittest import mock

from oah import secrets
from tests.helpers import make_tmp_dir, write_posix_tool


def fake_key() -> str:
    """A key-shaped value generated at runtime; never a real credential."""
    return "fake-" + token_source.token_hex(16)


# A stand-in for the macOS tool: items are files under {store}/<service>/<account>, every argv goes to {log},
# and a file named default-exit, find-exit or add-exit forces that exit status.
TOOL_SCRIPT = r'''
printf '%s\n' "$*" >> "{log}"
case "$1" in
  default-keychain)
    if [ -f "{store}/default-exit" ]; then exit "$(cat "{store}/default-exit")"; fi
    exit 0;;
  find-generic-password)
    [ "$2" = "-s" ] && [ "$4" = "-a" ] || exit 2
    if [ -f "{store}/find-exit" ]; then exit "$(cat "{store}/find-exit")"; fi
    [ -f "{store}/$3/$5" ] || exit 44
    if [ "$6" = "-w" ]; then cat "{store}/$3/$5"; printf '\n'; fi
    exit 0;;
  delete-generic-password)
    [ -f "{store}/$3/$5" ] || exit 44
    rm "{store}/$3/$5"
    exit 0;;
  -i)
    read -r command update s service a account w value rest || true
    [ "$command" = "add-generic-password" ] && [ "$update" = "-U" ] && [ "$s" = "-s" ] && [ "$a" = "-a" ] \
      && [ "$w" = "-w" ] && [ -z "$rest" ] || exit 2
    if [ -f "{store}/add-exit" ]; then exit "$(cat "{store}/add-exit")"; fi
    mkdir -p "{store}/$service"
    if [ -f "{store}/alter" ]; then value=altered; fi
    printf '%s' "$value" > "{store}/$service/$account"
    exit 0;;
esac
exit 2
'''


class RecordingStore:
    """A key store double: values in memory, and every call it received."""

    def __init__(self, values: dict[str, str] | None = None, state: str | None = None) -> None:
        self.values = dict(values or {})
        self.state = state
        self.calls: list[tuple[str, str]] = []

    def probe(self, name: str) -> tuple[str, str | None]:
        self.calls.append(("probe", name))
        if self.state is not None:
            return self.state, None
        value = self.values.get(name)
        return (secrets.PRESENT, value) if value else (secrets.MISSING, None)

    def lookup(self, name: str) -> str | None:
        return self.probe(name)[1]

    def store(self, name: str, value: str) -> str:
        self.calls.append(("store", name))
        if self.state is not None:
            raise secrets.KeyStoreError(self.state)
        existed = name in self.values
        self.values[name] = value
        return secrets.UPDATED if existed else secrets.ADDED

    def remove(self, name: str) -> str:
        self.calls.append(("remove", name))
        if self.state is not None:
            raise secrets.KeyStoreError(self.state)
        return secrets.REMOVED if self.values.pop(name, None) else secrets.MISSING


class LookupOnly:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def lookup(self, name: str) -> str | None:
        return self.values.get(name)


class FindStoreRemoveTest(unittest.TestCase):
    def test_find_reads_the_store_alone_and_names_the_state(self) -> None:
        value = fake_key()
        state, key = secrets.find("DEMO_NAME", keychain=RecordingStore({"DEMO_NAME": value}))
        self.assertEqual((state, key.reveal()), (secrets.PRESENT, value))
        self.assertEqual(secrets.find("DEMO_NAME", keychain=RecordingStore()), (secrets.MISSING, None))
        for refusal in (secrets.LOCKED, secrets.DENIED, secrets.UNSUPPORTED, secrets.FAILED):
            with self.subTest(state=refusal):
                self.assertEqual(secrets.find("DEMO_NAME", keychain=RecordingStore(state=refusal)), (refusal, None))
        self.assertEqual(secrets.find("DEMO_NAME", keychain=RecordingStore(state="surprise")), (secrets.FAILED, None))

    def test_find_never_reads_the_environment(self) -> None:
        name = "OAH_TEST_NAME_" + token_source.token_hex(4).upper()
        os.environ[name] = fake_key()
        self.addCleanup(os.environ.pop, name, None)
        self.assertEqual(secrets.find(name, keychain=RecordingStore()), (secrets.MISSING, None))

    def test_find_accepts_a_lookup_only_keychain(self) -> None:
        value = fake_key()
        state, key = secrets.find("DEMO_NAME", keychain=LookupOnly({"DEMO_NAME": value}))
        self.assertEqual((state, key.reveal()), (secrets.PRESENT, value))
        self.assertEqual(secrets.find("DEMO_NAME", keychain=LookupOnly({})), (secrets.MISSING, None))
        self.assertEqual(secrets.find("DEMO_NAME", keychain=secrets.NoKeychain()), (secrets.UNSUPPORTED, None))

    def test_store_adds_then_updates_and_remove_deletes(self) -> None:
        chain = RecordingStore()
        first, second = fake_key(), fake_key()
        self.assertEqual(secrets.store("DEMO_NAME", secrets.Secret(first), keychain=chain), secrets.ADDED)
        self.assertEqual(secrets.store("DEMO_NAME", secrets.Secret(second), keychain=chain), secrets.UPDATED)
        self.assertEqual(chain.values, {"DEMO_NAME": second})
        self.assertEqual(secrets.remove("DEMO_NAME", keychain=chain), secrets.REMOVED)
        self.assertEqual(secrets.remove("DEMO_NAME", keychain=chain), secrets.MISSING)

    def test_refusals_carry_a_state_and_never_the_value(self) -> None:
        value = fake_key()
        for refusal in (secrets.LOCKED, secrets.DENIED, secrets.UNSUPPORTED, secrets.FAILED):
            with self.subTest(state=refusal):
                with self.assertRaises(secrets.KeyStoreError) as caught:
                    secrets.store("DEMO_NAME", secrets.Secret(value), keychain=RecordingStore(state=refusal))
                self.assertEqual(caught.exception.state, refusal)
                self.assertNotIn(value, str(caught.exception) + repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                with self.assertRaises(secrets.KeyStoreError) as caught:
                    secrets.remove("DEMO_NAME", keychain=RecordingStore(state=refusal))
                self.assertEqual(caught.exception.state, refusal)

    def test_a_store_without_a_write_path_is_unsupported(self) -> None:
        for chain in (LookupOnly({}), secrets.NoKeychain()):
            with self.subTest(chain=type(chain).__name__):
                with self.assertRaises(secrets.KeyStoreError) as caught:
                    secrets.store("DEMO_NAME", secrets.Secret(fake_key()), keychain=chain)
                self.assertEqual(caught.exception.state, secrets.UNSUPPORTED)
                with self.assertRaises(secrets.KeyStoreError) as caught:
                    secrets.remove("DEMO_NAME", keychain=chain)
                self.assertEqual(caught.exception.state, secrets.UNSUPPORTED)

    def test_a_failing_store_reports_error_without_its_text(self) -> None:
        value = fake_key()

        class Broken(RecordingStore):
            def store(self, name: str, value: str) -> str:
                raise RuntimeError(f"cannot write {value}")

            def remove(self, name: str) -> str:
                raise RuntimeError("cannot remove")

        for action in (lambda: secrets.store("DEMO_NAME", secrets.Secret(value), keychain=Broken()),
                       lambda: secrets.remove("DEMO_NAME", keychain=Broken())):
            with self.assertRaises(secrets.KeyStoreError) as caught:
                action()
            self.assertEqual(caught.exception.state, secrets.FAILED)
            self.assertNotIn(value, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)

    def test_store_takes_a_secret_of_one_visible_line(self) -> None:
        chain = RecordingStore()
        value = fake_key()
        for bad in (value, None, b"bytes"):
            with self.subTest(kind=type(bad).__name__), self.assertRaises(ValueError) as caught:
                secrets.store("DEMO_NAME", bad, keychain=chain)
            self.assertNotIn(value, str(caught.exception))
        for text in (value + "\n", value + "\x00", "\x7f" + value, "a" * (secrets.VALUE_LIMIT + 1), "\ud800"):
            with self.subTest(length=len(text)), self.assertRaises(ValueError) as caught:
                secrets.store("DEMO_NAME", secrets.Secret(text), keychain=chain)
            self.assertNotIn(value, str(caught.exception))
        with self.assertRaises(ValueError):
            secrets.store("not a name", secrets.Secret(value), keychain=chain)
        self.assertEqual(chain.calls, [], "nothing reaches the store before the checks pass")

    def test_service_names_are_checked(self) -> None:
        for bad in ("", "has space", "-leading", "a" * 65, "oah:x", "oah/x", 7):
            with self.subTest(service=bad), self.assertRaises(ValueError):
                secrets.check_service(bad)
        for good in ("oah", "oah-selftest-0a1b2c3d", "a.b_c-d"):
            secrets.check_service(good)


class Terminal(io.BytesIO):
    def isatty(self) -> bool:
        return True

    def read(self, *args):
        raise AssertionError("a terminal is never read")


class TextTerminal:
    """Text standard input on a terminal, as sys.stdin is."""

    def __init__(self) -> None:
        self.buffer = Terminal()

    def isatty(self) -> bool:
        return True


class HandoffTest(unittest.TestCase):
    def test_reads_one_line_and_drops_one_line_end(self) -> None:
        value = fake_key()
        for ending in ("", "\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                self.assertEqual(secrets.read_handoff(io.BytesIO((value + ending).encode())).reveal(), value)

    def test_reads_through_a_text_wrapper_buffer(self) -> None:
        value = fake_key()
        stream = io.TextIOWrapper(io.BytesIO((value + "\n").encode()), encoding="utf-8")
        self.assertEqual(secrets.read_handoff(stream).reveal(), value)

    def test_a_terminal_is_refused_before_any_read(self) -> None:
        for stream in (Terminal(), TextTerminal()):
            with self.subTest(stream=type(stream).__name__), self.assertRaises(ValueError) as caught:
                secrets.read_handoff(stream)
            self.assertIn("OAH_SECRET_HANDOFF", str(caught.exception))

    def test_the_read_is_bounded(self) -> None:
        class Endless:
            def __init__(self) -> None:
                self.given = 0

            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    raise AssertionError("an unbounded read was asked for")
                self.given += size
                return b"a" * size

        stream = Endless()
        with self.assertRaises(ValueError) as caught:
            secrets.read_handoff(stream)
        self.assertIn("longer than", str(caught.exception))
        self.assertEqual(stream.given, secrets.HANDOFF_LIMIT + 1)
        self.assertEqual(secrets.read_handoff(io.BytesIO(b"a" * 64), limit=64).reveal(), "a" * 64)
        with self.assertRaises(ValueError):
            secrets.read_handoff(io.BytesIO(b"a" * 65), limit=64)

    def test_refusals_name_the_rule_and_never_the_bytes(self) -> None:
        value = fake_key()
        cases = {
            "empty": b"",
            "only a line end": b"\n",
            "two lines": (value + "\n" + value + "\n").encode(),
            "control character": (value + "\x1b[0m").encode(),
            "not UTF-8": value.encode() + b"\xff\xfe",
            "too long": (value * 200).encode(),
        }
        for label, data in cases.items():
            with self.subTest(case=label), self.assertRaises(ValueError) as caught:
                secrets.read_handoff(io.BytesIO(data))
            text = str(caught.exception) + repr(caught.exception)
            self.assertIn("OAH_SECRET_HANDOFF", text)
            self.assertNotIn(value, text)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__, "the decoding error would carry the bytes")

    def test_text_instead_of_bytes_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            secrets.read_handoff(io.StringIO(fake_key()))
        self.assertIn("bytes", str(caught.exception))


@unittest.skipIf(os.name == "nt", "the macOS keychain path runs a POSIX tool")
class MacStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir(self)
        self.items = self.tmp / "items"
        self.items.mkdir()
        self.log = self.tmp / "argv.log"
        self.bin = self.tmp / "bin"
        write_posix_tool(self.bin, "security", TOOL_SCRIPT.replace("{store}", str(self.items))
                         .replace("{log}", str(self.log)))
        self.chain = secrets.MacKeychain(search_path=str(self.bin))

    def argv(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_store_hands_the_value_over_on_standard_input_only(self) -> None:
        first, second = fake_key(), fake_key()
        self.assertEqual(secrets.store("DEMO_NAME", secrets.Secret(first), keychain=self.chain), secrets.ADDED)
        self.assertEqual((self.items / "oah" / "DEMO_NAME").read_text(encoding="utf-8"), first)
        self.assertEqual(secrets.store("DEMO_NAME", secrets.Secret(second), keychain=self.chain), secrets.UPDATED)
        self.assertEqual(self.chain.lookup("DEMO_NAME"), second)
        state, key = secrets.find("DEMO_NAME", keychain=self.chain)
        self.assertEqual((state, key.reveal()), (secrets.PRESENT, second))
        log = self.argv()
        self.assertIn("-i\n", log)
        self.assertNotIn(first, log)
        self.assertNotIn(second, log)
        self.assertNotIn("add-generic-password", log, "the add command itself goes on standard input")

    def test_remove_deletes_and_reports_missing(self) -> None:
        secrets.store("DEMO_NAME", secrets.Secret(fake_key()), keychain=self.chain)
        self.assertEqual(secrets.remove("DEMO_NAME", keychain=self.chain), secrets.REMOVED)
        self.assertEqual(self.chain.probe("DEMO_NAME"), (secrets.MISSING, None))
        self.assertEqual(secrets.remove("DEMO_NAME", keychain=self.chain), secrets.MISSING)

    def test_a_test_namespace_never_touches_the_oah_service(self) -> None:
        value = fake_key()
        other = secrets.MacKeychain(search_path=str(self.bin), service="oah-selftest-0a1b2c3d")
        secrets.store("DEMO_NAME", secrets.Secret(value), keychain=other)
        self.assertEqual(other.lookup("DEMO_NAME"), value)
        self.assertIsNone(self.chain.lookup("DEMO_NAME"))
        self.assertFalse((self.items / "oah").exists())
        with self.assertRaises(ValueError):
            secrets.MacKeychain(search_path=str(self.bin), service="oah selftest")

    def test_probe_tells_missing_from_locked_and_denied(self) -> None:
        (self.items / "oah").mkdir()
        (self.items / "oah" / "DEMO_NAME").write_text(fake_key(), encoding="utf-8")
        for code, state in ((44, secrets.MISSING), (36, secrets.LOCKED), (29, secrets.LOCKED), (51, secrets.DENIED),
                            (128, secrets.DENIED), (2, secrets.FAILED)):
            with self.subTest(exit=code):
                (self.items / "find-exit").write_text(str(code), encoding="utf-8")
                self.assertEqual(self.chain.probe("DEMO_NAME"), (state, None))
                self.assertEqual(secrets.find("DEMO_NAME", keychain=self.chain), (state, None))
                self.assertIsNone(self.chain.lookup("DEMO_NAME"))

    def test_probe_names_a_missing_tool_and_a_wait(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(secrets.MacKeychain(search_path=str(empty)).probe("DEMO_NAME"), (secrets.UNSUPPORTED, None))
        slow = self.tmp / "slow"
        write_posix_tool(slow, "security", "sleep 5")
        waiting = secrets.MacKeychain(search_path=str(slow), timeout=0.3)
        self.assertEqual(waiting.probe("DEMO_NAME"), (secrets.LOCKED, None))
        with self.assertRaises(secrets.KeyStoreError) as caught:
            secrets.store("DEMO_NAME", secrets.Secret(fake_key()), keychain=waiting)
        self.assertEqual(caught.exception.state, secrets.LOCKED)

    def test_a_locked_or_refusing_store_gets_no_value(self) -> None:
        value = fake_key()
        for code, state in ((36, secrets.LOCKED), (51, secrets.DENIED), (2, secrets.FAILED)):
            with self.subTest(exit=code):
                (self.items / "find-exit").write_text(str(code), encoding="utf-8")
                with self.assertRaises(secrets.KeyStoreError) as caught:
                    secrets.store("DEMO_NAME", secrets.Secret(value), keychain=self.chain)
                self.assertEqual(caught.exception.state, state)
        self.assertNotIn("-i\n", self.argv(), "the value is handed over only after the store answered")
        (self.items / "find-exit").unlink()
        for code, state in ((36, secrets.LOCKED), (51, secrets.DENIED), (45, secrets.FAILED)):
            with self.subTest(add_exit=code):
                (self.items / "add-exit").write_text(str(code), encoding="utf-8")
                with self.assertRaises(secrets.KeyStoreError) as caught:
                    secrets.store("DEMO_NAME", secrets.Secret(value), keychain=self.chain)
                self.assertEqual(caught.exception.state, state)
                self.assertNotIn(value, str(caught.exception))

    def test_no_default_keychain_is_unsupported_and_gets_no_value(self) -> None:
        (self.items / "default-exit").write_text("1", encoding="utf-8")
        with self.assertRaises(secrets.KeyStoreError) as caught:
            secrets.store("DEMO_NAME", secrets.Secret(fake_key()), keychain=self.chain)
        self.assertEqual(caught.exception.state, secrets.UNSUPPORTED)
        self.assertEqual(self.argv(), "default-keychain\n", "nothing else ran, so nothing could prompt")

    def test_store_reads_the_value_back(self) -> None:
        (self.items / "alter").write_text("1", encoding="utf-8")
        with self.assertRaises(secrets.KeyStoreError) as caught:
            secrets.store("DEMO_NAME", secrets.Secret(fake_key()), keychain=self.chain)
        self.assertEqual(caught.exception.state, secrets.FAILED)

    def test_a_value_the_tool_would_split_is_refused_before_it_runs(self) -> None:
        value = fake_key()
        for text in (value + " tail", value + '"', value + "'", value + "\\", value + "\u00e9", " " + value):
            with self.subTest(text=len(text)), self.assertRaises(ValueError) as caught:
                secrets.store("DEMO_NAME", secrets.Secret(text), keychain=self.chain)
            self.assertIn("OAH_SECRET_INVALID", str(caught.exception))
            self.assertNotIn(value, str(caught.exception))
        self.assertEqual(self.argv(), "")
        punctuation = "!#$%&()*+,-./:;<=>?@[]^_`{|}~"
        self.assertEqual(secrets.store("DEMO_NAME", secrets.Secret(value + punctuation), keychain=self.chain),
                         secrets.ADDED)
        self.assertEqual(self.chain.lookup("DEMO_NAME"), value + punctuation)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("OAH_KEY_STORE_SELFTEST") == "1",
                     "writes to the login keychain: set OAH_KEY_STORE_SELFTEST=1 to run it")
class MacNativeSmokeTest(unittest.TestCase):
    """The real tool and the real login keychain, in a namespace no product item uses, removed afterwards."""

    def test_add_update_read_and_remove_in_a_test_namespace(self) -> None:
        import pwd

        # The test package points HOME at a scratch folder, where the tool finds no keychain at all.
        self.enterContext(mock.patch.dict(os.environ, {"HOME": pwd.getpwuid(os.getuid()).pw_dir}))
        service = "oah-selftest-" + token_source.token_hex(4)
        chain = secrets.MacKeychain(timeout=5.0, service=service)
        name = "OAH_SELFTEST_VALUE"
        first, second = fake_key(), fake_key() + "!#$%&()*+,-./:;<=>?@[]^_{|}~"
        self.addCleanup(lambda: secrets.remove(name, keychain=chain))
        self.assertEqual(chain.probe(name), (secrets.MISSING, None))
        self.assertEqual(secrets.store(name, secrets.Secret(first), keychain=chain), secrets.ADDED)
        self.assertEqual(chain.probe(name), (secrets.PRESENT, first))
        self.assertEqual(secrets.store(name, secrets.Secret(second), keychain=chain), secrets.UPDATED)
        state, key = secrets.find(name, keychain=chain)
        self.assertEqual((state, key.reveal()), (secrets.PRESENT, second))
        self.assertEqual(secrets.remove(name, keychain=chain), secrets.REMOVED)
        self.assertEqual(chain.probe(name), (secrets.MISSING, None))
        self.assertEqual(secrets.remove(name, keychain=chain), secrets.MISSING)
        self.assertIsNone(secrets.MacKeychain(timeout=5.0).lookup(name), "the oah service was never written")


@unittest.skipUnless(os.name == "nt", "Windows Credential Manager is Windows-only")
class WindowsStoreTest(unittest.TestCase):
    """The real Credential Manager, in a namespace no product entry uses, kept to the logon session."""

    def setUp(self) -> None:
        self.chain = secrets.WindowsCredentials(service="oah-selftest-" + token_source.token_hex(4),
                                                persist=secrets.CRED_PERSIST_SESSION)
        self.name = "OAH_SELFTEST_" + token_source.token_hex(4).upper()

    def store(self, value: str) -> str:
        try:
            return secrets.store(self.name, secrets.Secret(value), keychain=self.chain)
        except secrets.KeyStoreError as exc:
            if exc.state == secrets.LOCKED:
                self.skipTest("this logon session has no credential set (error 1312), as over SSH with a key")
            raise

    def test_add_update_read_and_remove(self) -> None:
        first, second = fake_key(), fake_key() + "키🔑 with space"
        self.assertEqual(self.chain.probe(self.name), (secrets.MISSING, None))
        self.assertEqual(self.store(first), secrets.ADDED)
        self.addCleanup(lambda: self.chain.remove(self.name) if self.chain.probe(self.name)[0] == secrets.PRESENT else None)
        self.assertEqual(self.chain.probe(self.name), (secrets.PRESENT, first))
        self.assertEqual(self.store(second), secrets.UPDATED)
        state, key = secrets.find(self.name, keychain=self.chain)
        self.assertEqual((state, key.reveal()), (secrets.PRESENT, second))
        self.assertIsNone(secrets.WindowsCredentials().lookup(self.name), "the oah target was never written")
        self.assertEqual(secrets.remove(self.name, keychain=self.chain), secrets.REMOVED)
        self.assertEqual(self.chain.probe(self.name), (secrets.MISSING, None))
        self.assertEqual(secrets.remove(self.name, keychain=self.chain), secrets.MISSING)

    def test_a_blob_over_the_limit_is_refused(self) -> None:
        with self.assertRaises(secrets.KeyStoreError) as caught:
            self.chain.store(self.name, "a" * (secrets.CRED_BLOB_LIMIT // 2 + 1))
        self.assertEqual(caught.exception.state, secrets.FAILED)
        self.assertEqual(self.chain.probe(self.name)[0], secrets.MISSING)


@unittest.skipIf(os.name == "nt", "off Windows the Credential Manager path answers unsupported")
class WindowsElsewhereTest(unittest.TestCase):
    def test_every_call_is_unsupported_off_windows(self) -> None:
        chain = secrets.WindowsCredentials(service="oah-selftest-0a1b2c3d", persist=secrets.CRED_PERSIST_SESSION)
        self.assertEqual(chain.probe("DEMO_NAME"), (secrets.UNSUPPORTED, None))
        self.assertIsNone(chain.lookup("DEMO_NAME"))
        for action in (lambda: chain.store("DEMO_NAME", fake_key()), lambda: chain.remove("DEMO_NAME")):
            with self.assertRaises(secrets.KeyStoreError) as caught:
                action()
            self.assertEqual(caught.exception.state, secrets.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
