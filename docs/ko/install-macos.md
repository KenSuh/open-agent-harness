# macOS에 설치하기

macOS 13 이상과 터미널 창만 있으면 된다. 명령 하나가 나머지를 진행하고 무엇이든 설치하기 전에 먼저 묻는다.

## 1. 설치 방법 고르기

방법은 두 가지다. 어느 쪽이든 같은 설치 프로그램이 같은 질문을 한다.

**클론에서 설치한다.** 저장소가 비공개인 지금은 이 방법을 쓴다. 먼저 `git`이 있어야 한다.
`git --version`이 실패하면 `xcode-select --install`을 실행하고 열리는 창에서 설치를 끝낸다. 그다음 아래
명령을 실행한다.

```bash
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
bash install/install.sh
```

**명령 한 줄로 설치한다.** 릴리스가 공개된 뒤에 쓸 수 있고 클론도 `git`도 필요 없다.

```bash
curl -fsSL https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.sh | bash
```

`<version>` 자리에는 릴리스 페이지(https://github.com/KenSuh/open-agent-harness/releases)에 있는 버전을
넣는다.

이 방법은 고정된 릴리스 압축 파일을 내려받는다. 그리고 릴리스와 함께 공개된 SHA-256 지문과 맞춰 본다.
지문이 다르면 아무것도 풀지 않고 멈춘다(`OAH_INSTALL_DIGEST_MISMATCH`). 압축을 푼 파일은 임시 폴더에만
두고 끝날 때 지운다. 릴리스가 아직 없으면 `OAH_INSTALL_NOT_PUBLISHED`를 보여 주고 멈춘다. 그때는 클론
방법을 쓴다. 이 방법으로 옵션을 주려면 `bash -s --` 뒤에 적는다. 예를 들면 다음과 같다.

```bash
curl -fsSL https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.sh | bash -s -- --dry-run
```

## 2. 설치 프로그램이 하는 일

아무것도 바꾸지 않고 단계만 먼저 보려면 `--dry-run`을 붙인다. 설치 프로그램은 순서가 있는 계획을 만들고
출력만 한다. 도구나 버전 검사, 안내식 설정을 실행하거나 프로필을 읽지 않는다. 설치된 도구는 실제 실행에서만
확인하므로 계획에는 조건별 명령이 함께 나온다. 공개 설치 방식은 릴리스 다운로드, SHA-256 검사, 압축 해제와
정리 계획도 보여 준다. 임시 폴더를 만들거나 압축 파일을 내려받거나 검증하지 않는다. 동의하면서 계획을
실행하려면 `--dry-run` 없이 다시 실행한다. 실제 릴리스 다운로드와 모든 리디렉션은 HTTPS만 허용한다.

설치는 다섯 단계로 진행된다.

1. **Python 3.11 이상을 찾는다.** 없으면 **uv**를 권한다. uv는 Python을 대신 내려받아 준다. 설치
   프로그램은 공식 명령(`curl -LsSf https://astral.sh/uv/install.sh | sh`)을 보여 주고 실행하기 전에
   묻는다. 하위 프로세스에 `UV_NO_MODIFY_PATH=1`을 주므로 uv는 셸 프로필이나 영구 PATH 설정을 고치지
   않는다. 이를 바꾸려면 4단계의 별도 질문에 동의한다.
2. **`oah`가 쓰는 도구를 확인한다.** 찾은 도구와 없는 도구를 표로 보여 주고 설치 계획을 이어서 보여 준다.
   빠진 도구마다 **공식 설치 명령을 그대로** 보여 준다. 짧은 안내와 공식 페이지도 함께 보여 주고
   `Install now? [y/N]`라고 묻는다. `y`를 입력해야만 설치하고 다른 답은 건너뛴다. 비밀번호가 필요한
   설치 프로그램은 직접 비밀번호를 묻는다. `oah`는 비밀번호를 보지 않는다.
3. **`oah`를 설치한다.** uv가 있으면 `uv tool install --force <이 폴더>`로 설치한다. uv가 없으면
   `pipx`를 쓰고 그것도 없으면 마지막으로 `python -m pip install --user`를 쓴다.
4. **새 터미널이 `oah`를 찾는지 확인한다.** 새 터미널 창이 열릴 때와 같은 방식으로 셸을 띄워 `oah`를
   찾아본다. 찾지 못하면 `uv tool update-shell`을 권한다. 이 명령은 셸 프로필을 고친다. 그래서 `y`라고
   답할 때만 실행한다. pipx나 pip로 설치했다면 `~/.zprofile`에 넣을 한 줄을 대신 보여 준다.
5. **`oah doctor`를 실행한다.** 모든 도구를 나열하고 빠진 도구마다 설치 명령을 알려 준다.

모두 갖춰지면 마지막 줄이 `OAH_INSTALL_OK`다. 필수 도구 가운데 아직 없거나 쓸 수 없는 것이 있으면
`OAH_INSTALL_INCOMPLETE`가 나오고 그 위의 목록이 할 일을 알려 준다. 끝나면 새 터미널 창을 열고 프로젝트
폴더로 가서 `oah init`을 실행한다.

### 옵션

| 옵션 | 하는 일 |
|---|---|
| `--yes` | 하나씩 묻지 않고 빠진 도구를 모두 설치한다. 그래도 셸 프로필은 고치지 않는다. |
| `--dry-run` | 모든 단계를 보여 주기만 하고 아무것도 바꾸지 않는다. |
| `--skip-tools` | 2단계를 건너뛰고 `oah`만 설치한다. |

## 3. Claude Code와 Codex에 로그인하기

2단계에서 로그인 상태도 확인한다. 로그인되어 있지 않으면 다음 순서를 안내한다.

1. **새** 터미널 창을 연다.
2. 안내된 명령을 실행한다. Claude Code는 `claude auth login`이고 Codex는 `codex login`이다.
3. 브라우저에서 로그인을 마치고 돌아와 Enter를 누른다.

그러면 각 도구의 상태 명령(`claude auth status`, `codex login status`)으로 다시 확인한다. 비밀번호와
키는 그 도구에만 남는다. `s`를 입력해 건너뛰고 나중에 로그인해도 된다. `oah doctor`가 언제든 다시
확인한다.

Claude Code를 쓰려면 Pro, Max, Team, Enterprise 또는 Console 계정이 있어야 한다. Codex는 ChatGPT 요금제
로그인이나 API 키로 쓸 수 있다.

## 도구 목록

| 도구 | 필요 여부 | macOS 공식 명령 |
|---|---|---|
| git | 필수 | `brew install git` 또는 `xcode-select --install` |
| Python 3.11+ | 필수 | `uv python install 3.13` 또는 `brew install python@3.13` |
| Claude Code | 필수 | `curl -fsSL https://claude.ai/install.sh \| bash` |
| Codex CLI | 필수 | `brew install --cask codex` 또는 `npm install -g @openai/codex` |
| uv | 권장 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 권장, 게이트를 쓰는 프로젝트에서는 필수 | `brew install node` |
| unlazy | 권장 (게이트) | P5가 반영된 뒤 `oah init --with-unlazy`로 P5 계약 검사를 통과한 고정 커밋 설치 |
| graphify | 권장 | `uv tool install graphifyy` |
| gh | 권장 | `brew install gh` |
| gitleaks | 권장 | `brew install gitleaks` |
| jq | 권장 | `brew install jq` |
| ripgrep (`rg`) | 권장 | `brew install ripgrep` |

`brew`로 시작하는 명령은 [Homebrew](https://brew.sh)가 있어야 실행된다. Homebrew가 없으면 안내 화면이
그 도구의 다운로드 페이지를 대신 보여 준다. `brew install node`는 현재 Node.js 릴리스를 설치하고 PATH에
올린다. unlazy는 Node 16 이상에서 돌아간다.

이번 단계의 도구 설치 안내는 unlazy를 설치하지 않는다. P5가 반영된 뒤 `oah init --with-unlazy`를 쓴다.
게이트 명령을 실행하기 전에 계약 검사를 통과한 고정 커밋과 스크립트 지문을 검증한다.

프로젝트가 게이트를 쓰기 시작하면 `oah doctor`는 Node를 필수로 센다. `.oah/tasks/*/GATES.md` 파일이
있거나 `.oah/config.toml`에 `[gates]` 섹션이 있으면 게이트를 쓰는 것으로 본다. 단 그 섹션에
`enabled = false`가 있으면 쓰지 않는 것으로 본다. 그 전까지 Node는 권장 도구다.

Aside(브라우저 작업)와 Orca(데스크톱 앱)는 꼭 필요하지 않다. 쓰고 싶으면 각자의 다운로드 페이지에서
설치한다.

## 문제가 생기면

- **설치한 뒤 `oah: command not found`가 나온다.** 새 터미널 창을 연다. 그래도 안 되면
  `uv tool update-shell`을 실행하거나 설치 프로그램이 보여 준 줄을 `~/.zprofile`에 넣는다.
- **pip가 "externally managed environment"라고 한다.** 위의 명령으로 uv를 설치하고 설치 프로그램을
  다시 실행한다.
- **어떤 도구의 설치가 실패했다.** 그 도구가 남긴 메시지를 읽고 원인을 고친 뒤 `bash install/install.sh`를
  다시 실행한다. 이미 설치된 도구는 건드리지 않는다.
- **전체를 다시 확인하고 싶다.** `oah doctor`를 실행한다.
