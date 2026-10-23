# 설정 로더 검사 — .oah/config.toml 의 기본값 병합, 모르는 키·타입 오류, CRLF·BOM, 결과 격리를 본다.
from __future__ import annotations

import pathlib
import unittest

from oah.config import CONFIG_RELATIVE_PATH, Config, ConfigError, load_config
from tests.helpers import make_tmp_dir

TIERS = {"low": {"model": "sonnet", "effort": "low", "timeout_min": 15},
         "high": {"model": "opus", "effort": "high", "timeout_min": 60}}


def write_config(test: unittest.TestCase, text: str | bytes) -> pathlib.Path:
    root = make_tmp_dir(test)
    path = root.joinpath(*CONFIG_RELATIVE_PATH.parts)
    path.parent.mkdir(parents=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return root


class LoadTest(unittest.TestCase):
    def test_relative_path_constant(self):
        self.assertEqual(CONFIG_RELATIVE_PATH, pathlib.PurePosixPath(".oah/config.toml"))

    def test_missing_file_gives_empty_config(self):
        config = load_config(make_tmp_dir(self))
        self.assertIsNone(config.path)
        self.assertEqual(dict(config.raw), {})
        self.assertEqual(config.sections(), ())
        self.assertEqual(config.section("lease", {"ttl_hours": 4.0}), {"ttl_hours": 4.0})

    def test_path_names_the_file_read(self):
        root = write_config(self, "[lease]\nttl_hours = 2.0\n")
        self.assertEqual(load_config(root).path, root / ".oah" / "config.toml")

    def test_crlf_file(self):
        root = write_config(self, '[stages]\r\ninitial = "claude"\r\nreview = "codex"\r\n')
        self.assertEqual(load_config(root).section("stages", {"initial": "", "review": ""}),
                         {"initial": "claude", "review": "codex"})

    def test_utf8_bom_file(self):
        root = write_config(self, b'\xef\xbb\xbf[stages]\nreview = "\xec\xbd\x94\xeb\x8d\xb1\xec\x8a\xa4"\n')
        self.assertEqual(load_config(root).section("stages", {"review": ""}), {"review": "코덱스"})

    def test_invalid_toml_raises_config_invalid(self):
        root = write_config(self, "[stages\ninitial = \n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(root)
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn(str(root / ".oah" / "config.toml"), str(ctx.exception))

    def test_non_utf8_raises_config_invalid(self):
        root = write_config(self, b'[stages]\nreview = "\xc4\xda\xb5\xa6"\n')  # cp949 bytes
        with self.assertRaises(ConfigError) as ctx:
            load_config(root)
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("UTF-8", str(ctx.exception))

    def test_unreadable_path_raises_config_invalid(self):
        root = make_tmp_dir(self)
        (root / ".oah" / "config.toml").mkdir(parents=True)
        with self.assertRaises(ConfigError) as ctx:
            load_config(root)
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")

    def test_raw_is_read_only(self):
        config = load_config(write_config(self, "[a]\nb = [1, 2]\n[a.c]\nd = 1\n"))
        with self.assertRaises(TypeError):
            config.raw["x"] = 1  # type: ignore[index]
        with self.assertRaises(TypeError):
            config.raw["a"]["c"]["d"] = 2
        with self.assertRaises(AttributeError):
            config.raw["a"]["b"].append(3)

    def test_sections_lists_top_level_tables(self):
        config = load_config(write_config(self, 'top = 1\n[stages]\nx = 1\n[engines.claude]\ncommand = ["claude"]\n'))
        self.assertEqual(config.sections(), ("stages", "engines"))


class SectionTest(unittest.TestCase):
    def section(self, text: str, name: str, defaults: dict) -> dict:
        return load_config(write_config(self, text)).section(name, defaults)

    def assert_error(self, code: str, text: str, name: str, defaults: dict, *fragments: str) -> None:
        root = write_config(self, text)
        with self.assertRaises(ConfigError) as ctx:
            load_config(root).section(name, defaults)
        self.assertEqual(ctx.exception.code, code)
        message = str(ctx.exception)
        self.assertTrue(message.startswith(code + ": "), message)
        self.assertIn(str(root / ".oah" / "config.toml"), message)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_missing_section_returns_copy_of_defaults(self):
        defaults = {"command": ["claude"], "tiers": TIERS}
        result = self.section("[other]\nx = 1\n", "engine", defaults)
        self.assertEqual(result, defaults)
        self.assertIsNot(result["tiers"], TIERS)
        self.assertIsNot(result["command"], defaults["command"])

    def test_deep_merge_over_defaults(self):
        text = '[engine]\ncommand = ["python", "fake.py"]\n[engine.tiers.high]\nmodel = "fable"\n'
        result = self.section(text, "engine", {"command": ["claude"], "tiers": TIERS})
        self.assertEqual(result["command"], ["python", "fake.py"])
        self.assertEqual(result["tiers"]["high"], {"model": "fable", "effort": "high", "timeout_min": 60})
        self.assertEqual(result["tiers"]["low"], TIERS["low"])

    def test_inline_table_merges_like_a_table(self):
        text = '[engine.tiers]\nlow = { effort = "medium" }\n'
        result = self.section(text, "engine", {"tiers": TIERS})
        self.assertEqual(result["tiers"]["low"], {"model": "sonnet", "effort": "medium", "timeout_min": 15})

    def test_unknown_key_raises(self):
        self.assert_error("OAH_CONFIG_UNKNOWN_KEY", "[stages]\nreveiw = \"codex\"\n", "stages",
                          {"initial": "claude", "review": "codex"}, "stages.reveiw", "initial", "review")
        self.assert_error("OAH_CONFIG_UNKNOWN_KEY", "[engine.tiers.low]\nmodle = \"x\"\n", "engine",
                          {"tiers": TIERS}, "engine.tiers.low.modle")

    def test_empty_defaults_table_is_open_ended(self):
        text = '[engine.tiers]\nfast = { model = "haiku", effort = "low" }\n'
        result = self.section(text, "engine", {"command": ["claude"], "tiers": {}})
        self.assertEqual(result, {"command": ["claude"], "tiers": {"fast": {"model": "haiku", "effort": "low"}}})
        self.assertEqual(self.section("[free]\nanything = [1, \"a\"]\n", "free", {}), {"anything": [1, "a"]})

    def test_type_mismatch_raises(self):
        self.assert_error("OAH_CONFIG_INVALID", "[engine.tiers.low]\ntimeout_min = \"15\"\n", "engine",
                          {"tiers": TIERS}, "engine.tiers.low.timeout_min", "an integer", "a string")
        self.assert_error("OAH_CONFIG_INVALID", "[engine]\ntiers = 3\n", "engine", {"tiers": TIERS},
                          "engine.tiers", "a table")
        self.assert_error("OAH_CONFIG_INVALID", "[engine.command]\nx = 1\n", "engine", {"command": ["claude"]},
                          "engine.command", "an array")

    def test_int_accepted_for_float(self):
        result = self.section("[lease]\nttl_hours = 2\n", "lease", {"ttl_hours": 4.0})
        self.assertEqual(result, {"ttl_hours": 2.0})
        self.assertIs(type(result["ttl_hours"]), float)

    def test_float_rejected_for_int(self):
        self.assert_error("OAH_CONFIG_INVALID", "[lease]\nretries = 2.5\n", "lease", {"retries": 3}, "lease.retries")

    def test_bool_is_never_an_int(self):
        self.assert_error("OAH_CONFIG_INVALID", "[lease]\nretries = true\n", "lease", {"retries": 3},
                          "lease.retries", "true or false")
        self.assert_error("OAH_CONFIG_INVALID", "[lease]\nttl_hours = false\n", "lease", {"ttl_hours": 4.0},
                          "lease.ttl_hours")
        self.assert_error("OAH_CONFIG_INVALID", "[review]\nstrict = 1\n", "review", {"strict": True},
                          "review.strict")

    def test_none_default_accepts_any_value(self):
        self.assertEqual(self.section("[x]\nmodel = \"opus\"\n", "x", {"model": None}), {"model": "opus"})
        self.assertEqual(self.section("[y]\n", "x", {"model": None}), {"model": None})

    def test_array_default_accepts_array(self):
        result = self.section('[engine]\ncommand = ["codex", "--flag"]\n', "engine", {"command": ("codex",)})
        self.assertEqual(result, {"command": ["codex", "--flag"]})

    def test_non_table_section_raises(self):
        self.assert_error("OAH_CONFIG_INVALID", "stages = \"claude\"\n", "stages", {"initial": "claude"},
                          "stages", "a table")

    def test_result_is_isolated_from_defaults_and_cache(self):
        config = load_config(write_config(self, '[engine]\ncommand = ["x"]\n[engine.tiers.low]\nmodel = "m"\n'))
        defaults = {"command": ["claude"], "tiers": {"low": {"model": "sonnet", "args": ["a"]}}, "extra": {}}
        first = config.section("engine", defaults)
        first["command"].append("mutated")
        first["tiers"]["low"]["model"] = "mutated"
        first["tiers"]["low"]["args"].append("mutated")
        first["extra"]["k"] = "mutated"
        self.assertEqual(defaults, {"command": ["claude"], "tiers": {"low": {"model": "sonnet", "args": ["a"]}},
                                    "extra": {}})
        self.assertEqual(config.section("engine", defaults),
                         {"command": ["x"], "tiers": {"low": {"model": "m", "args": ["a"]}}, "extra": {}})
        missing = config.section("absent", defaults)
        missing["tiers"]["low"]["args"].append("mutated")
        self.assertEqual(defaults["tiers"]["low"]["args"], ["a"])

    def test_config_can_be_built_directly(self):
        self.assertEqual(Config(path=None, raw={}).section("s", {"a": 1}), {"a": 1})

    def test_direct_config_raw_is_read_only(self):
        config = Config(path=None, raw={"s": {"items": [{"values": [1]}],
                                              "tuple_items": ({"values": [2]},)}})
        with self.assertRaises(TypeError):
            config.raw["other"] = {}  # type: ignore[index]
        with self.assertRaises(TypeError):
            config.raw["s"]["items"][0]["values"] = ()
        with self.assertRaises(AttributeError):
            config.raw["s"]["items"].append(3)
        with self.assertRaises(AttributeError):
            config.raw["s"]["items"][0]["values"].append(3)
        with self.assertRaises(TypeError):
            config.raw["s"]["tuple_items"][0]["values"] = ()
        with self.assertRaises(AttributeError):
            config.raw["s"]["tuple_items"][0]["values"].append(3)
        self.assertEqual(config.section("s", {}),
                         {"items": [{"values": [1]}], "tuple_items": [{"values": [2]}]})

    def test_direct_config_is_isolated_from_original_input(self):
        raw = {"s": {"items": [{"values": [1]}], "tuple_items": ({"values": [2]},)}}
        config = Config(path=None, raw=raw)
        raw["s"]["items"][0]["values"].append(3)
        raw["s"]["items"].append({"values": [4]})
        raw["s"]["tuple_items"][0]["values"].append(5)
        raw["s"]["added"] = True
        raw["other"] = {}
        self.assertEqual(config.sections(), ("s",))
        self.assertEqual(config.section("s", {}),
                         {"items": [{"values": [1]}], "tuple_items": [{"values": [2]}]})


if __name__ == "__main__":
    unittest.main()
