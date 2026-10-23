# Windows 에 설치하기

Windows 11(Windows 10 22H2 는 가능한 범위에서 지원)과 PowerShell 이 있으면 됩니다. WSL 은 필요
없습니다. 명령 하나로 나머지를 진행하며, 무엇이든 설치하기 전에 먼저 묻습니다.

## 1. 설치 방법 고르기

두 가지 방법이 있습니다. 둘 다 같은 설치 프로그램을 실행하고 같은 질문을 합니다.

**클론에서 설치 (저장소가 비공개인 지금 쓰는 방법).** 먼저 Git 이 필요합니다. `git --version` 이
실패하면 `winget install --id Git.Git -e` 를 실행하고 새 PowerShell 창을 엽니다. 그다음 이렇게 합니다.

```powershell
git clone <저장소 주소> open-agent-harness
cd open-agent-harness
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

`-ExecutionPolicy Bypass` 는 이번 실행에만 적용되며 시스템 설정은 바뀌지 않습니다. 원한다면
`Unblock-File .\install\install.ps1` 을 한 번 실행한 뒤 평소 정책으로 실행해도 됩니다.

**명령 한 줄로 설치 (릴리스가 공개된 뒤).** 클론도 Git 도 필요 없습니다.

```powershell
irm <릴리스 주소>/install.ps1 | iex
```

이 방법은 고정된 릴리스 압축 파일을 내려받아, 릴리스와 함께 공개된 SHA-256 지문과 비교합니다. 다르면
아무것도 풀지 않고 멈춥니다(`OAH_INSTALL_DIGEST_MISMATCH`). 푼 파일은 모두 임시 폴더에 두었다가 끝날 때
지우며, 무슨 일이 있어도 PowerShell 창은 닫히지 않습니다. 릴리스가 아직 공개되지 않았으면
`OAH_INSTALL_NOT_PUBLISHED` 로 멈추니, 그때는 클론 방법을 씁니다. 이 방법에서 옵션을 주려면
`& ([scriptblock]::Create((irm <릴리스 주소>/install.ps1))) -DryRun` 처럼 실행합니다.

## 2. 설치 프로그램이 하는 일

아무것도 바꾸지 않고 단계만 먼저 보려면 `-DryRun` 을 붙입니다.

설치는 다섯 단계로 진행됩니다.

1. **Python 3.11 이상 찾기.** `py -3`, `python`, `python3` 순서로 찾습니다. 없으면 Python 을 대신
   내려받아 주는 **uv** 를 제안합니다. 공식 명령(`irm https://astral.sh/uv/install.ps1 | iex`)을 보여
   주고, 실행하기 전에 묻습니다.
2. **`oah` 가 쓰는 도구 확인.** 있는 도구와 없는 도구를 표로 보여 준 다음 설치 계획을 보여 줍니다.
   빠진 도구마다 **공식 설치 명령 그대로**, 짧은 안내, 공식 페이지를 보여 주고 `Install now? [y/N]` 라고
   묻습니다. `y` 를 입력해야만 설치하고, 다른 답은 건너뜁니다. 설치할 때마다 PATH 를 다시 읽으므로 새
   도구를 같은 창에서 바로 쓸 수 있습니다.
3. **`oah` 설치.** uv 가 있으면 `uv tool install --force <이 폴더>` 로 설치합니다. uv 가 없으면
   `pipx` 를, 그것도 없으면 마지막으로 `python -m pip install --user` 를 씁니다.
4. **새 PowerShell 창이 `oah` 를 찾는지 확인.** 새 창이 설정에서 받아 오는 PATH 에서 `oah` 를 찾아봅니다.
   찾지 못하면 `uv tool update-shell` 을 제안합니다. 이 명령은 사용자 PATH 설정에 그 폴더를 더하므로 `y`
   라고 답할 때만 실행합니다. pipx 나 pip 로 설치했다면 직접 실행할 PowerShell 명령을 대신 보여 줍니다.
5. **`oah doctor` 실행.** 모든 도구를 나열하고, 빠진 도구마다 설치 명령을 알려 줍니다.

모두 준비되면 마지막 줄이 `OAH_INSTALL_OK` 이고, 필수 도구가 아직 준비되지 않았으면
`OAH_INSTALL_INCOMPLETE` 입니다. 그 위의 목록이 무엇을 하면 되는지 알려 줍니다. 그다음 새 PowerShell
창을 열고 프로젝트 폴더로 가서 `oah init` 을 실행합니다.

### 옵션

| 옵션 | 하는 일 |
|---|---|
| `-Yes` | 하나씩 묻지 않고 빠진 도구를 모두 설치합니다. 그래도 PATH 설정은 바꾸지 않습니다. |
| `-DryRun` | 모든 단계를 출력만 하고 아무것도 바꾸지 않습니다. |
| `-SkipTools` | 2단계를 건너뛰고 `oah` 만 설치합니다. |

## 3. Claude Code 와 Codex 로그인

2단계에서 로그인 상태도 확인합니다. 로그인되어 있지 않으면 이렇게 안내합니다.

1. **새** PowerShell 창을 엽니다.
2. 안내된 명령을 실행합니다. Claude Code 는 `claude auth login`, Codex 는 `codex login` 입니다.
3. 브라우저에서 로그인을 마치고 돌아와 Enter 를 누릅니다.

그러면 각 도구의 상태 명령(`claude auth status`, `codex login status`)으로 다시 확인합니다. 비밀번호와
키는 그 도구들에만 남습니다. `s` 로 건너뛰고 나중에 로그인해도 되며, `oah doctor` 가 언제든 확인합니다.

Claude Code 는 Pro, Max, Team, Enterprise 또는 Console 계정이 필요합니다. Codex 는 ChatGPT 요금제
로그인이나 API 키를 받습니다.

## 도구 목록

| 도구 | 필요 여부 | Windows 공식 명령 |
|---|---|---|
| Git | 필수 | `winget install --id Git.Git -e --source winget` |
| Python 3.11+ | 필수 | `uv python install 3.13` 또는 `winget install Python.Python.3.13` |
| Claude Code | 필수 | `irm https://claude.ai/install.ps1 \| iex` 또는 `winget install Anthropic.ClaudeCode` |
| Codex CLI | 필수 | `irm https://chatgpt.com/codex/install.ps1 \| iex` |
| uv | 권장 | `irm https://astral.sh/uv/install.ps1 \| iex` |
| Node.js | 권장, 게이트를 쓰는 프로젝트에서는 필수 | `winget install OpenJS.NodeJS.LTS` |
| unlazy | 권장 (게이트) | `npx skills add Leonxlnx/unlazy` |
| graphify | 권장 | `uv tool install graphifyy` |
| gh | 권장 | `winget install --id GitHub.cli --source winget` |
| gitleaks | 권장 | `scoop install gitleaks` ([Scoop](https://scoop.sh) 필요) |
| jq | 권장 | `winget install jqlang.jq` |
| ripgrep (`rg`) | 권장 | `winget install BurntSushi.ripgrep.MSVC` |

`irm … | iex` 한 줄 명령은 별도의 Windows PowerShell 프로세스에서 실행됩니다.

`oah doctor` 는 프로젝트가 게이트를 쓰기 시작하면 Node 를 필수로 셉니다. `.oah/tasks/*/GATES.md` 파일이
있거나, `.oah/config.toml` 에 `enabled = false` 가 아닌 `[gates]` 섹션이 있을 때입니다. 그 전에는 권장
도구입니다. Aside(브라우저 작업)와
Orca(데스크톱 앱)는 선택 사항이며 각자의 다운로드 페이지에서 설치합니다.

## 문제가 생기면

- **설치 뒤 `oah` 를 찾을 수 없다고 한다.** 새 PowerShell 창을 엽니다. 그래도 안 되면
  `uv tool update-shell` 이나 설치 프로그램이 보여 준 명령을 실행합니다.
- **`winget` 을 찾을 수 없다고 한다.** Microsoft Store 에서 "앱 설치 관리자(App Installer)" 를 설치한 뒤
  설치 프로그램을 다시 실행합니다.
- **"이 시스템에서 스크립트를 실행할 수 없으므로" 라는 오류가 난다.** 위에 적힌 그대로
  `-ExecutionPolicy Bypass` 를 붙여 실행합니다.
- **어떤 도구의 설치가 실패했다.** 그 메시지를 읽고 원인을 고친 뒤 설치 프로그램을 다시 실행합니다. 이미
  설치된 도구는 건드리지 않습니다.
- **전체를 다시 확인하려면** `oah doctor` 를 실행합니다.
