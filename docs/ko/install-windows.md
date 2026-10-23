# Windows에 설치하기

Windows 11과 PowerShell만 있으면 된다. Windows 10 22H2에서도 대체로 돌아가지만 보장하지는 않는다. WSL은
필요 없다. 명령 하나가 나머지를 진행하고 무엇이든 설치하기 전에 먼저 묻는다.

## 1. 설치 방법 고르기

방법은 두 가지다. 어느 쪽이든 같은 설치 프로그램이 같은 질문을 한다.

**클론에서 설치한다.** 저장소가 비공개인 지금은 이 방법을 쓴다. 먼저 Git이 있어야 한다.
`git --version`이 실패하면 `winget install --id Git.Git -e`를 실행하고 새 PowerShell 창을 연다. 그다음
아래 명령을 실행한다.

```powershell
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

`-ExecutionPolicy Bypass`는 이번 실행에만 적용되고 시스템 설정은 바뀌지 않는다. 원하면
`Unblock-File .\install\install.ps1`을 한 번 실행한 뒤 평소 정책으로 실행해도 된다.

**명령 한 줄로 설치한다.** 릴리스가 공개된 뒤에 쓸 수 있고 클론도 Git도 필요 없다.

```powershell
irm https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.ps1 | iex
```

`<version>` 자리에는 릴리스 페이지(https://github.com/KenSuh/open-agent-harness/releases)에 있는 버전을
넣는다.

이 방법은 고정된 릴리스 압축 파일을 내려받는다. 그리고 릴리스와 함께 공개된 SHA-256 지문과 맞춰 본다.
지문이 다르면 아무것도 풀지 않고 멈춘다(`OAH_INSTALL_DIGEST_MISMATCH`). 압축을 푼 파일은 임시 폴더에만
두고 끝날 때 지운다. 무슨 일이 생겨도 PowerShell 창은 닫히지 않는다. 릴리스가 아직 없으면
`OAH_INSTALL_NOT_PUBLISHED`를 보여 주고 멈춘다. 그때는 클론 방법을 쓴다. 이 방법으로 옵션을 주려면 다음처럼
실행한다.

```powershell
& ([scriptblock]::Create((irm https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.ps1))) -DryRun
```

## 2. 설치 프로그램이 하는 일

아무것도 바꾸지 않고 단계만 먼저 보려면 `-DryRun`을 붙인다.

설치는 다섯 단계로 진행된다.

1. **Python 3.11 이상을 찾는다.** `py -3`, `python`, `python3` 순서로 찾는다. 없으면 **uv**를 권한다.
   uv는 Python을 대신 내려받아 준다. 설치 프로그램은 공식 명령(`irm https://astral.sh/uv/install.ps1 | iex`)을
   보여 주고 실행하기 전에 묻는다.
2. **`oah`가 쓰는 도구를 확인한다.** 찾은 도구와 없는 도구를 표로 보여 주고 설치 계획을 이어서 보여 준다.
   빠진 도구마다 **공식 설치 명령을 그대로** 보여 준다. 짧은 안내와 공식 페이지도 함께 보여 주고
   `Install now? [y/N]`라고 묻는다. `y`를 입력해야만 설치하고 다른 답은 건너뛴다. 도구를 하나 설치할
   때마다 PATH를 다시 읽는다. 그래서 새 도구를 같은 창에서 바로 쓸 수 있다.
3. **`oah`를 설치한다.** uv가 있으면 `uv tool install --force <이 폴더>`로 설치한다. uv가 없으면
   `pipx`를 쓰고 그것도 없으면 마지막으로 `python -m pip install --user`를 쓴다.
4. **새 PowerShell 창이 `oah`를 찾는지 확인한다.** 새 창은 설정에 저장된 PATH를 받는다. 그 PATH에서
   `oah`를 찾아본다. 찾지 못하면 `uv tool update-shell`을 권한다. 이 명령은 사용자 PATH 설정에 그 폴더를
   더한다. 그래서 `y`라고 답할 때만 실행한다. pipx나 pip로 설치했다면 직접 실행할 PowerShell 명령을 대신
   보여 준다.
5. **`oah doctor`를 실행한다.** 모든 도구를 나열하고 빠진 도구마다 설치 명령을 알려 준다.

모두 갖춰지면 마지막 줄이 `OAH_INSTALL_OK`다. 필수 도구 가운데 아직 없거나 쓸 수 없는 것이 있으면
`OAH_INSTALL_INCOMPLETE`가 나오고 그 위의 목록이 할 일을 알려 준다. 끝나면 새 PowerShell 창을 열고 프로젝트
폴더로 가서 `oah init`을 실행한다.

### 옵션

| 옵션 | 하는 일 |
|---|---|
| `-Yes` | 하나씩 묻지 않고 빠진 도구를 모두 설치한다. 그래도 PATH 설정은 바꾸지 않는다. |
| `-DryRun` | 모든 단계를 보여 주기만 하고 아무것도 바꾸지 않는다. |
| `-SkipTools` | 2단계를 건너뛰고 `oah`만 설치한다. |

## 3. Claude Code와 Codex에 로그인하기

2단계에서 로그인 상태도 확인한다. 로그인되어 있지 않으면 다음 순서를 안내한다.

1. **새** PowerShell 창을 연다.
2. 안내된 명령을 실행한다. Claude Code는 `claude auth login`이고 Codex는 `codex login`이다.
3. 브라우저에서 로그인을 마치고 돌아와 Enter를 누른다.

그러면 각 도구의 상태 명령(`claude auth status`, `codex login status`)으로 다시 확인한다. 비밀번호와
키는 그 도구에만 남는다. `s`를 입력해 건너뛰고 나중에 로그인해도 된다. `oah doctor`가 언제든 다시
확인한다.

Claude Code를 쓰려면 Pro, Max, Team, Enterprise 또는 Console 계정이 있어야 한다. Codex는 ChatGPT 요금제
로그인이나 API 키로 쓸 수 있다.

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

`irm … | iex` 형태의 공식 명령은 별도의 Windows PowerShell 프로세스에서 실행된다.

프로젝트가 게이트를 쓰기 시작하면 `oah doctor`는 Node를 필수로 센다. `.oah/tasks/*/GATES.md` 파일이
있거나 `.oah/config.toml`에 `[gates]` 섹션이 있으면 게이트를 쓰는 것으로 본다. 단 그 섹션에
`enabled = false`가 있으면 쓰지 않는 것으로 본다. 그 전까지 Node는 권장 도구다.

Aside(브라우저 작업)와 Orca(데스크톱 앱)는 꼭 필요하지 않다. 쓰고 싶으면 각자의 다운로드 페이지에서
설치한다.

## 문제가 생기면

- **설치한 뒤 `oah`를 찾을 수 없다고 한다.** 새 PowerShell 창을 연다. 그래도 안 되면
  `uv tool update-shell`이나 설치 프로그램이 보여 준 명령을 실행한다.
- **`winget`을 찾을 수 없다고 한다.** Microsoft Store에서 "앱 설치 관리자(App Installer)"를 설치한 뒤
  설치 프로그램을 다시 실행한다.
- **"이 시스템에서 스크립트를 실행할 수 없으므로"라는 오류가 난다.** 위에 적힌 그대로
  `-ExecutionPolicy Bypass`를 붙여 실행한다.
- **어떤 도구의 설치가 실패했다.** 그 도구가 남긴 메시지를 읽고 원인을 고친 뒤 설치 프로그램을 다시
  실행한다. 이미 설치된 도구는 건드리지 않는다.
- **전체를 다시 확인하고 싶다.** `oah doctor`를 실행한다.
