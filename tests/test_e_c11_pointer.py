from __future__ import annotations

import datetime as dt
from unittest import mock

from tests import e_c6_fixture, e_c9_fixture, test_e_c10_boundaries as boundary_fixture
from tests.test_e_c10_boundaries import files


class PointerReasonTest(e_c9_fixture.CorrectionFixture):
    prepare_ordinary_after_managed = boundary_fixture.FixBoundaryTest.prepare_ordinary_after_managed

    def check_pointer_message(self, project, earlier, *, reason):
        before, calls = files(earlier.run.path), project.fix_calls()

        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertEqual((code, result["code"]), (1, "OAH_FIX_STATE_CHANGED"), str(result) + err)
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(files(earlier.run.path), before)
        self.assertNotIn("Traceback", err)
        if reason:
            self.assertIn("start a new initial run", result["message"].lower())
            self.assertIn(reason, result["message"].lower())
            self.assertNotIn("inspect the task records and retry", result["message"])
        else:
            self.assertIn("inspect the task records and retry", result["message"])

    def test_absent_original_names_new_initial_remedy(self):
        from oah.discovery import original

        project, earlier, _need, _basis = e_c6_fixture.selected_ordinary(self)
        original._index(project.state, "t1").unlink()

        self.check_pointer_message(project, earlier, reason="no managed initial run is recorded")

    def test_ordinary_original_retirement_names_new_initial_remedy(self):
        project, earlier, _kept, _ordinary = self.prepare_ordinary_after_managed(
            new_lease=False, restore_selection=True)

        self.check_pointer_message(project, earlier, reason="ordinary initial run retired")

    def test_other_changed_inputs_keep_the_generic_refusal(self):
        project, earlier, _need, _basis = e_c6_fixture.selected_ordinary(self)
        earlier.run.prompt_path.write_text("fixture prompt changed before freeze", encoding="utf-8")

        self.check_pointer_message(project, earlier, reason=None)

    def test_non_enum_pointer_reason_is_dropped_and_uses_generic_literal(self):
        from oah.discovery import original
        from oah.review import ReviewError, fix

        project, earlier, _need, _basis = e_c6_fixture.selected_ordinary(self)
        original._index(project.state, "t1").unlink()
        refusal, diagnostic, seen = original.OriginalUnavailable, fix._error_diagnostic, []

        def wrong_reason(reason):
            exc = refusal(reason)
            setattr(exc, "pointer_reason", "fixture non-enum reason")
            return exc

        def checked(exc):
            if isinstance(exc, ReviewError):
                seen.append(hasattr(exc, "pointer_reason"))
            return diagnostic(exc)

        with mock.patch.object(original, "OriginalUnavailable", wrong_reason), \
                mock.patch.object(fix, "_error_diagnostic", checked):
            self.check_pointer_message(project, earlier, reason=None)

        self.assertTrue(seen)
        self.assertFalse(any(seen))
