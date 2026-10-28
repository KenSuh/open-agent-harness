# 예제 프로그램 — 인사말을 출력한다. 따라 하기 작업에서 이름을 받도록 고친다.
"""Print a greeting. The worked example's task teaches it to greet a name."""
import sys


def greet() -> str:
    return "Hello, world!"


def main(argv: list[str]) -> int:
    print(greet())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
