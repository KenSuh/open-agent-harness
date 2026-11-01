# Windows에 설치하기

Windows 11과 PowerShell만 있으면 된다. Windows 10 22H2에서도 대체로 돌아가지만 보장하지는 않는다. WSL은
필요 없다. 명령 하나가 나머지를 진행하고 무엇이든 설치하기 전에 먼저 묻는다.

## 1. 설치 방법 고르기

방법은 두 가지다. 어느 쪽이든 같은 설치 프로그램이 같은 질문을 한다.

**클론에서 설치한다.** 먼저 Git이 있어야 한다.
`git --version`이 실패하면 `winget install --id Git.Git -e`를 실행하고 새 PowerShell 창을 연다. 그다음
아래 명령을 실행한다.

```powershell
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

`-ExecutionPolicy Bypass`는 이번 실행에만 적용되고 시스템 설정은 바뀌지 않는다. 원하면
`Unblock-File .\install\install.ps1`을 한 번 실행한 뒤 평소 정책으로 실행해도 된다.

**v0.1.0 릴리스에서 설치한다.** 저장소의 릴리스 페이지에서 해당 태그의 소스 압축 파일과 `SHA256SUMS`를
받는다. `SHA256SUMS`에 적힌 정확한 파일을 쓰고 다른 압축 형식으로 바꾸지 않는다. 클론은 필요 없지만 git은
여전히 oah의 필수 도구다.

압축을 풀기 전에 SHA-256을 계산한다. `<downloaded-archive>`는 받은 파일 이름으로 바꾼다.

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath ".\<downloaded-archive>"
```

전체 지문을 릴리스의 `SHA256SUMS`에서 해당 파일의 값과 비교한다. 16진수 문자의 대소문자는 무관하다.
**일치할 때만 압축을 풀고 설치 프로그램을 실행한다.** 지문이 다르거나 항목이 없으면 멈추고 릴리스 페이지에서
서로 맞는 압축 파일과 지문 파일을 다시 받거나 클론 방법을 쓴다. 검증한 뒤 압축을 풀고 소스 폴더에서 터미널을
열어 안에 있는 설치 프로그램을 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

설치 프로그램 소스에 적힌 릴리스 링크 형식은
`https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.ps1`이다.
실제 공개된 파일은 저장소의 릴리스 페이지에서 찾는다. 체크아웃 사본에는 아직 릴리스 핀 자리표시자가 있으므로
위의 검증된 소스 압축 파일 방법은 별도 설치 파일이 공개되었다고 가정하지 않는다.

## 2. 설치 프로그램이 하는 일

아무것도 바꾸지 않고 단계만 먼저 보려면 `-DryRun`을 붙인다. 설치 프로그램은 순서가 있는 계획을 만들고
출력만 한다. 도구나 버전 검사, 안내식 설정을 실행하거나 PATH를 갱신하지 않는다. 설치된 도구는 실제 실행에서만
확인하므로 계획에는 조건별 명령이 함께 나온다. 직접 받은 압축 파일의 지문을 검증해 주지는
않으므로 위의 지문 확인을 먼저 마친다. 동의하면서 계획을 실행하려면 `-DryRun` 없이 다시 실행한다. 실제 릴리스 다운로드와 모든 리디렉션은 HTTPS만 허용한다.

설치는 다섯 단계로 진행된다.

1. **Python 3.11 이상을 찾는다.** `py -3`, `python`, `python3` 순서로 찾는다. 없으면 **uv**를 권한다.
   uv는 Python을 대신 내려받아 준다. 설치 프로그램은 공식 명령(`irm https://astral.sh/uv/install.ps1 | iex`)을
   보여 주고 실행하기 전에 묻는다. 하위 프로세스에 `UV_NO_MODIFY_PATH=1`을 주므로 uv는 프로필이나
   영구 PATH 설정을 고치지 않는다. uv 폴더는 현재 프로세스의 PATH에만 더해 바로 쓸 수 있게 한다.
   저장된 PATH를 바꾸려면 4단계의 별도 질문에 동의한다.
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
| unlazy | 권장 (게이트) | `oah init --with-unlazy`로 `oah` 게이트를 검증한 고정 커밋 설치 |
| graphify | 권장 | `uv tool install graphifyy` |
| gh | 권장 | `winget install --id GitHub.cli --source winget` |
| gitleaks | 권장 | `scoop install gitleaks` ([Scoop](https://scoop.sh) 필요) |
| jq | 권장 | `winget install jqlang.jq` |
| ripgrep (`rg`) | 권장 | `winget install BurntSushi.ripgrep.MSVC` |

`irm … | iex` 형태의 공식 명령은 별도의 Windows PowerShell 프로세스에서 실행된다.

설치 프로그램은 unlazy를 설치하지 않는다. 프로젝트 안에서 `oah init --with-unlazy`가 설치한다. `oah` 게이트를
검증한 커밋을 설치하고, 게이트 명령을 실행하기 전에 그 커밋과 스크립트 지문을 검증한다.

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
