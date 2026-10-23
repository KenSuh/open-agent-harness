# 콘솔 출력 안전성 검사 — cp949 처럼 좁은 인코딩으로 리다이렉트돼도 출력이 죽지 않는지 본다.
import io
import unittest

from oah.console import make_safe


class MakeSafeTest(unittest.TestCase):
    def test_cp949_stream_survives_emoji_and_replacement_char(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp949", newline="\n")
        safe = make_safe(stream)
        safe.write("한글 \U0001f600 �\n")
        safe.flush()
        text = raw.getvalue().decode("cp949")
        self.assertIn("한글", text)
        self.assertIn("\\U0001f600", text)
        self.assertIn("\\ufffd", text)

    def test_stream_without_reconfigure_is_rewrapped(self):
        raw = io.BytesIO()

        class LegacyStream:
            encoding = "cp949"
            buffer = raw

            def write(self, text):
                raise AssertionError("the unsafe stream must not be written to")

        safe = make_safe(LegacyStream())
        safe.write("ok \U0001f600\n")
        safe.flush()
        self.assertIn("ok \\U0001f600", raw.getvalue().decode("cp949"))

    def test_stringio_is_returned_unchanged(self):
        stream = io.StringIO()
        self.assertIs(make_safe(stream), stream)


if __name__ == "__main__":
    unittest.main()
