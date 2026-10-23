# 내 프로젝트용 CI 스타터

`oah ci init` 은 프로젝트에 **지속적 통합(CI)** 을 붙여 줍니다. GitHub 에 코드를 올리거나(push) 풀 리퀘스트를
열 때마다, GitHub 가 새 컴퓨터에서 테스트를 돌리고 저장소 이력에 키가 새어 들어가지 않았는지 검사합니다.
모두 통과하면 초록 체크가, 고칠 것이 있으면 빨간 X 가 보입니다.

[English](../ci-starter.md)

## 설정하기

프로젝트 안에서 실행합니다(git 저장소 안의 어느 폴더든 됩니다).

```sh
oah ci init
```

1. oah 가 테스트 명령을 찾고([테스트 명령을 찾는 방법](#테스트-명령을-찾는-방법) 참고) 그대로 쓸지 묻습니다.
   Enter 를 누르면 그대로 쓰고, `n` 이라고 답하면 직접 명령을 입력할 수 있습니다.
2. 워크플로 파일 `.github/workflows/oah-ci.yml` 전체를 화면에 보여 줍니다.
3. `Write .github/workflows/oah-ci.yml? [y/N]` 라고 묻습니다. `y` 라고 답하지 않으면 아무것도 쓰지 않습니다.
4. 파일을 커밋하고 push 합니다. 그 push 부터 GitHub 가 검사를 시작합니다.

옵션은 다음과 같습니다.

| 옵션 | 하는 일 |
|---|---|
| `--os macos,windows,linux` | 테스트할 운영체제. 기본값은 지금 쓰는 운영체제입니다. |
| `--test-command "make test"` | 찾은 명령 대신 이 테스트 명령을 씁니다. |
| `--dry-run` | 워크플로를 보여 주기만 하고 아무것도 쓰지 않습니다. 질문도 하지 않습니다. |
| `--yes` | 찾은 명령을 받아들이고 묻지 않고 씁니다. |
| `--force` | `--yes` 와 함께 쓰면 이미 있는 워크플로 파일을 바꿉니다. |

매번 옵션을 쓰기 싫다면 `.oah/config.toml` 에 적어 둘 수 있습니다.

```toml
[ci]
os = ["macos", "windows"]
test_command = "make test"
```

명령줄 옵션이 설정 파일보다 우선합니다.

### 파일이 이미 있으면

oah 는 워크플로 파일을 몰래 덮어쓰지 않습니다. `.github/workflows/oah-ci.yml` 이 이미 있으면 diff
(`-` 로 시작하는 줄은 사라지고 `+` 로 시작하는 줄은 새로 생깁니다)를 보여 주고, 바꾸기 전에 묻습니다.
`--yes` 만으로는 바꾸지 않고 거절합니다. 묻지 않고 바꾸는 방법은 `--force --yes` 하나뿐입니다. 파일이
이미 oah 가 쓸 내용과 같으면 그렇다고 알려 주고 아무것도 바꾸지 않습니다.

## 워크플로가 하는 일

워크플로는 모든 `push` 와 모든 `pull_request` 에서 돌고, 작업(job) 두 개로 되어 있습니다.

- **test** 는 고른 운영체제(`macos-latest`, `windows-latest`, `ubuntu-latest`)마다 한 번씩 돕니다. 코드를
  받고, 프로젝트에 필요한 언어 도구(Python, Node.js, Go. Rust 는 GitHub 컴퓨터에 이미 있습니다)를 설치한 뒤
  테스트 명령을 실행합니다. 명령마다 단계(step)가 따로 있어서, 어느 명령이 실패했는지 로그에서 바로 보입니다.
- **secret scan** 은 [gitleaks](https://github.com/gitleaks/gitleaks) 를 내려받고, 실행하기 전에 내려받은
  파일이 정확히 그 파일인지 SHA-256 지문으로 확인한 다음, **git 이력 전체**에서 비밀번호나 API 키처럼 보이는
  것을 찾습니다. `--redact --no-banner` 를 쓰고, 발견한 줄의 문맥을 출력하지 않으며 보고서도 쓰지 않아
  옆에 있는 값이 문맥을 통해 새어 나가지 않습니다.
  [고정 버전의 옵션](https://github.com/gitleaks/gitleaks/blob/v8.30.1/README.md#usage)을 참고하세요.

할 수 있는 일도 좁혀 두었습니다.

- `permissions: contents: read` 라서 워크플로는 코드를 읽기만 하고 다른 것은 할 수 없습니다.
- 쓰는 액션(`actions/checkout`, `actions/setup-python` 등)은 모두 정확한 커밋 하나에 고정되어 있습니다.
  그래서 원본 쪽이 바뀌어도 내가 파일을 고치기 전에는 내 프로젝트에서 도는 내용이 바뀌지 않습니다.
- 시크릿이나 토큰을 쓰지 않고, 저장소 설정도 바꾸지 않습니다.

## 테스트 명령을 찾는 방법

| 프로젝트에 있는 것 | oah 가 제안하는 명령 |
|---|---|
| `pyproject.toml`, `setup.cfg`, 또는 Python 파일이 든 `tests/` 폴더가 있고 pytest 가 설정되어 있음 | `python -m pip install pytest` 다음 `python -m pytest` |
| 위와 같지만 pytest 가 없고 `tests/` 바로 아래에 `test*.py` 파일이 있음 | `python -m unittest discover -s tests` |
| `pyproject.toml` 또는 `setup.cfg` 가 있고 pytest 없이 프로젝트 루트에 `test*.py` 파일이 있음 | `python -m unittest discover` |
| `test` 스크립트가 있는 `package.json` | `npm ci` 다음 `npm test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |

Python 은 `requirements.txt` 가 있으면 먼저 설치하고, `[project]` 표가 있는 `pyproject.toml` 은
`python -m pip install -e .` 로 설치합니다. 프로젝트에 `pytest.ini`, `conftest.py`,
`[tool.pytest.ini_options]` 표, `setup.cfg` 의 `[tool:pytest]` 절이 있거나 의존성에 pytest 가 있으면
pytest 가 설정된 것으로 봅니다.

unittest 는 위 두 테스트 파일 배치 중 하나가 있을 때만 제안합니다. `tests/__init__.py` 는 없어도 됩니다.
테스트 파일 이름은 유효한 Python 모듈 이름이어야 합니다. 다른 배치에는 일부러 실패하는 단계를 넣으니,
push 하기 전에 테스트 명령으로 바꾸세요.

`npm ci` 에는 `package-lock.json` 이 필요합니다. `npm install` 을 한 번 실행하고 lock 파일을 커밋하세요.

여러 가지에 해당하면 oah 가 목록을 보여 주고 하나를 고르게 합니다. 아무것도 해당하지 않으면 워크플로에
**Run tests (edit me)** 라는 단계가 들어갑니다. 이 단계는 눈에 띄도록 일부러 안내 메시지와 함께 실패합니다.
`.github/workflows/oah-ci.yml` 을 열어 그 단계의 `run:` 아래 세 줄을 테스트를 돌리는 명령으로 바꾸고 다시
push 하세요.

## GitHub 에서 결과 읽기

1. GitHub 에서 저장소를 열고 **Actions** 탭을 누릅니다. push 와 풀 리퀘스트마다 `oah-ci` 실행이 하나씩 있습니다.
2. 초록 체크는 모든 작업이 통과했다는 뜻이고, 빨간 X 는 하나 이상 실패했다는 뜻입니다.
3. 실행을 누르고, 실패한 작업(예: **test (windows-latest)**)을 누른 다음, 빨간 X 가 있는 단계를 누릅니다.
   로그에 테스트가 낸 오류 메시지가 있습니다.
4. 풀 리퀘스트에서는 대화 맨 아래에 같은 검사들이 나옵니다. **Details** 를 누르면 로그가 열립니다.

GitHub 안내: [워크플로 실행 로그 사용](https://docs.github.com/ko/actions/how-tos/monitor-workflows/use-workflow-run-logs).

**secret scan** 이 실패했다면 키가 커밋되었을 수 있습니다. 새 커밋에서 지우는 것만으로는 부족합니다. 이력에
남아 있기 때문입니다. 먼저 그 키를 발급한 서비스에서 키를 폐기하거나 새로 바꾸고, 그다음에 정리하세요. 키는
환경 변수나 운영체제 키체인에 두고, 프로젝트 파일에는 절대 넣지 마세요.

## 검사를 필수로 만들기

기본 상태에서 GitHub 는 결과를 보여 주기만 하고, 검사가 실패한 풀 리퀘스트도 병합할 수 있게 둡니다. 이를
막으려면 기본 브랜치(보통 `main`)에서 검사를 **필수**로 만드세요. oah 는 저장소 설정을 바꾸지 않습니다.
GitHub 에서 직접 한 번만 하면 됩니다.

1. 워크플로를 push 하고 한 번 돌게 둡니다. 그래야 GitHub 가 검사 이름을 압니다.
2. 저장소의 **Settings** 를 엽니다. 왼쪽 사이드바의 "Code, planning, and automation" 아래에서
   **Rulesets** 를 누르고, **New ruleset** → **New branch ruleset** 을 누릅니다.
3. 이름을 정하고 적용 상태(enforcement status)를 **Active** 로 둡니다. **Target branches** 에서
   **Add a target** → **Include default branch** 를 고릅니다.
4. **Require status checks to pass before merging** 을 켜고, 검사 이름을 추가합니다. `secret scan` 하나와
   운영체제마다 `test (…)` 하나씩입니다(예: `test (macos-latest)`).
5. **Create** 를 누릅니다.

GitHub 안내: [리포지토리에 대한 규칙 세트 만들기](https://docs.github.com/ko/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository),
그리고 예전 방식인 [분기 보호 규칙](https://docs.github.com/ko/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).
규칙 세트는 공개 저장소에서는 모든 요금제에서, 비공개 저장소에서는 GitHub Pro·Team·Enterprise Cloud 에서
쓸 수 있습니다.

## 나중에 바꾸기

파일은 내 것이니 마음대로 고쳐도 됩니다. `oah ci init` 을 다시 실행하면 고친 파일과 비교한 diff 를 보여 주고,
바꾸기 전에 묻습니다. 새 스타터가 어떻게 생겼는지 보고만 싶다면 `oah ci init --dry-run` 을 쓰세요.
