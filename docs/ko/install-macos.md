# macOS 에 설치하기

macOS 13 이상과 터미널 창이 있으면 됩니다. 명령 하나로 나머지를 진행하며, 무엇이든 설치하기 전에 먼저 묻습니다.

## 1. 설치 방법 고르기

두 가지 방법이 있습니다. 둘 다 같은 설치 프로그램을 실행하고 같은 질문을 합니다.

**클론에서 설치 (저장소가 비공개인 지금 쓰는 방법).** 먼저 `git` 이 필요합니다. `git --version` 이
실패하면 `xcode-select --install` 을 실행하고 열리는 창의 설치를 끝냅니다. 그다음 이렇게 합니다.

```bash
git clone <저장소 주소> open-agent-harness
cd open-agent-harness
bash install/install.sh
```

**명령 한 줄로 설치 (릴리스가 공개된 뒤).** 클론도 `git` 도 필요 없습니다.

```bash
curl -fsSL <릴리스 주소>/install.sh | bash
```

이 방법은 고정된 릴리스 압축 파일을 내려받아, 릴리스와 함께 공개된 SHA-256 지문과 비교합니다. 다르면
아무것도 풀지 않고 멈춥니다(`OAH_INSTALL_DIGEST_MISMATCH`). 푼 파일은 모두 임시 폴더에 두었다가 끝날 때
지웁니다. 릴리스가 아직 공개되지 않았으면 `OAH_INSTALL_NOT_PUBLISHED` 로 멈추니, 그때는 클론 방법을
씁니다. 이 방법에서 옵션을 주려면 `bash -s --` 뒤에 적습니다. 예를 들면
`curl -fsSL <릴리스 주소>/install.sh | bash -s -- --dry-run` 입니다.

## 2. 설치 프로그램이 하는 일

아무것도 바꾸지 않고 단계만 먼저 보려면 `--dry-run` 을 붙입니다.

설치는 다섯 단계로 진행됩니다.

1. **Python 3.11 이상 찾기.** 없으면 Python 을 대신 내려받아 주는 **uv** 를 제안합니다. 공식 명령
   (`curl -LsSf https://astral.sh/uv/install.sh | sh`)을 보여 주고, 실행하기 전에 묻습니다.
2. **`oah` 가 쓰는 도구 확인.** 있는 도구와 없는 도구를 표로 보여 준 다음 설치 계획을 보여 줍니다.
   빠진 도구마다 **공식 설치 명령 그대로**, 짧은 안내, 공식 페이지를 보여 주고 `Install now? [y/N]` 라고
   묻습니다. `y` 를 입력해야만 설치하고, 다른 답은 건너뜁니다. 비밀번호가 필요한 설치 프로그램은 직접
   여러분에게 묻습니다. `oah` 는 비밀번호를 보지 않습니다.
3. **`oah` 설치.** uv 가 있으면 `uv tool install --force <이 폴더>` 로 설치합니다. uv 가 없으면
   `pipx` 를, 그것도 없으면 마지막으로 `python -m pip install --user` 를 씁니다.
4. **새 터미널이 `oah` 를 찾는지 확인.** 새 터미널 창이 시작될 때처럼 셸을 띄워 `oah` 를 찾아봅니다.
   찾지 못하면 `uv tool update-shell` 을 제안합니다. 이 명령은 셸 프로필을 고치므로 `y` 라고 답할 때만
   실행합니다. pipx 나 pip 로 설치했다면 `~/.zprofile` 에 넣을 한 줄을 대신 보여 줍니다.
5. **`oah doctor` 실행.** 모든 도구를 나열하고, 빠진 도구마다 설치 명령을 알려 줍니다.

모두 준비되면 마지막 줄이 `OAH_INSTALL_OK` 이고, 필수 도구가 아직 준비되지 않았으면
`OAH_INSTALL_INCOMPLETE` 입니다. 그 위의 목록이 무엇을 하면 되는지 알려 줍니다. 그다음 새 터미널 창을
열고 프로젝트 폴더로 가서 `oah init` 을 실행합니다.

### 옵션

| 옵션 | 하는 일 |
|---|---|
| `--yes` | 하나씩 묻지 않고 빠진 도구를 모두 설치합니다. 그래도 셸 프로필은 고치지 않습니다. |
| `--dry-run` | 모든 단계를 출력만 하고 아무것도 바꾸지 않습니다. |
| `--skip-tools` | 2단계를 건너뛰고 `oah` 만 설치합니다. |

## 3. Claude Code 와 Codex 로그인

2단계에서 로그인 상태도 확인합니다. 로그인되어 있지 않으면 이렇게 안내합니다.

1. **새** 터미널 창을 엽니다.
2. 안내된 명령을 실행합니다. Claude Code 는 `claude auth login`, Codex 는 `codex login` 입니다.
3. 브라우저에서 로그인을 마치고 돌아와 Enter 를 누릅니다.

그러면 각 도구의 상태 명령(`claude auth status`, `codex login status`)으로 다시 확인합니다. 비밀번호와
키는 그 도구들에만 남습니다. `s` 로 건너뛰고 나중에 로그인해도 되며, `oah doctor` 가 언제든 확인합니다.

Claude Code 는 Pro, Max, Team, Enterprise 또는 Console 계정이 필요합니다. Codex 는 ChatGPT 요금제
로그인이나 API 키를 받습니다.

## 도구 목록

| 도구 | 필요 여부 | macOS 공식 명령 |
|---|---|---|
| git | 필수 | `brew install git` 또는 `xcode-select --install` |
| Python 3.11+ | 필수 | `uv python install 3.13` 또는 `brew install python@3.13` |
| Claude Code | 필수 | `curl -fsSL https://claude.ai/install.sh \| bash` |
| Codex CLI | 필수 | `brew install --cask codex` 또는 `npm install -g @openai/codex` |
| uv | 권장 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 권장, 게이트를 쓰는 프로젝트에서는 필수 | `brew install node` |
| unlazy | 권장 (게이트) | `npx skills add Leonxlnx/unlazy` |
| graphify | 권장 | `uv tool install graphifyy` |
| gh | 권장 | `brew install gh` |
| gitleaks | 권장 | `brew install gitleaks` |
| jq | 권장 | `brew install jq` |
| ripgrep (`rg`) | 권장 | `brew install ripgrep` |

`brew` 로 시작하는 명령은 [Homebrew](https://brew.sh) 가 있어야 합니다. 없으면 안내 화면이 그 도구의
다운로드 페이지를 대신 보여 줍니다. `brew install node` 는 현재 Node.js 릴리스를 설치하고 PATH 에
올립니다. unlazy 는 Node 16 이상이 필요합니다.

`oah doctor` 는 프로젝트가 게이트를 쓰기 시작하면 Node 를 필수로 셉니다. `.oah/tasks/*/GATES.md` 파일이
있거나, `.oah/config.toml` 에 `enabled = false` 가 아닌 `[gates]` 섹션이 있을 때입니다. 그 전에는 권장
도구입니다.

Aside(브라우저 작업)와 Orca(데스크톱 앱)는 선택 사항이며 각자의 다운로드 페이지에서 설치합니다.

## 문제가 생기면

- **설치 뒤 `oah: command not found` 가 나온다.** 새 터미널 창을 엽니다. 그래도 안 되면
  `uv tool update-shell` 을 실행하거나, 설치 프로그램이 보여 준 줄을 `~/.zprofile` 에 넣습니다.
- **pip 가 "externally managed environment" 라고 한다.** 위의 명령으로 uv 를 설치하고 설치 프로그램을
  다시 실행합니다.
- **어떤 도구의 설치가 실패했다.** 그 메시지를 읽고 원인을 고친 뒤 `bash install/install.sh` 를 다시
  실행합니다. 이미 설치된 도구는 건드리지 않습니다.
- **전체를 다시 확인하려면** `oah doctor` 를 실행합니다.
