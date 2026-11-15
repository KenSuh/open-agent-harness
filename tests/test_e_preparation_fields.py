from __future__ import annotations

import copy
import json

from tests.e_c9_fixture import CorrectionFixture, reseal


class PreparationFieldsTest(CorrectionFixture):
    def test_frozen_field_validation_precedes_any_use_of_malformed_dispatch_values(self):
        from oah.discovery import preparation
        from oah.discovery.store import digest

        project, original, _now, _parent, _relayed = self.selected_parent(relay=True)
        saved = json.loads(original.run.file_path("skills.json").read_bytes())["prepared_e"]
        before = original.run.file_path("skills.json").read_bytes()
        calls = project.fix_calls()
        cases = (
            (("expires_at",), "2026-11-10T10:00:00"),
            (("facts", "lease", "expires_at"), "2026-11-10T10:00:00"),
            (("profile", "observed_at"), "2026-11-10T10:00:00"),
            (("profile", "expires_at"), "not-an-ISO-time"),
            (("facts", "inventory", "parent", "observed_at"), "2026-11-10T10:00:00"),
            (("facts", "inventory", "parent", "expires_at"), "not-an-ISO-time"),
            (("facts", "context", "axes"), None),
            (("facts", "operation", "task"), None),
            (("facts", "operation", "lease"), {}),
            (("facts", "selectors"), []),
            (("facts", "request_budget"), "1"),
            (("references", "tier"), []),
            (("envelope", "prompt_bytes"), "1"),
            (("facts", "target", "model"), []),
        )

        for path, value in cases:
            with self.subTest(field=".".join(path)):
                record = copy.deepcopy(saved)
                parent = record
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                record["profile"]["id"] = digest({key: entry for key, entry in record["profile"].items() if key != "id"})
                record["facts"]["profile"] = digest(record["profile"])
                reseal(record)

                with self.assertRaises(ValueError):
                    preparation.parse(record)

        self.assertEqual(original.run.file_path("skills.json").read_bytes(), before)
        self.assertEqual(project.fix_calls(), calls)
