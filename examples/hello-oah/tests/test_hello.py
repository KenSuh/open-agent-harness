# 예제 프로그램의 테스트 — 기본 인사말을 확인한다.
import unittest

from hello import greet


class GreetTest(unittest.TestCase):
    def test_default_greeting(self):
        self.assertEqual(greet(), "Hello, world!")


if __name__ == "__main__":
    unittest.main()
