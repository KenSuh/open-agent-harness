# 업그레이드

`oah`는 그 아래의 모델과 스킬이 바뀌는 만큼 함께 바뀐다. 이 문서는 부분마다 업데이트하는 방법을 설명한다.
`oah` 자체, `oah init`이 프로젝트에 쓴 파일, 스킬 키트 캐시, 프로젝트에 고정된 스킬, MCP 템플릿, 모델 순서다.
마지막으로 `docs/UPSTREAM_SYNC.md`가 무엇을 기록하는지 설명한다.

[English](../upgrade.md)

먼저 열려 있는 리스를 끝내거나 내려놓는다(`oah status`가 목록을 보여 준다). 리스가 하나라도 열려 있으면 스킬
업데이트는 기다린다.

## 1. oah 자체 업데이트

**클론에서 설치했다면**(저장소가 비공개인 동안의 설치 방법이다) 새로 받아서 설치 프로그램을 다시 실행한다.
설치된 `oah`가 체크아웃의 버전으로 바뀐다.

```sh
cd open-agent-harness
git pull
bash install/install.sh --skip-tools
```

Windows에서는 이렇게 한다.

```powershell
cd open-agent-harness
git pull
powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -SkipTools
```

`--skip-tools`(`-SkipTools`)는 도구 확인을 건너뛰고 `oah`만 다시 설치한다. 도구도 확인하려면 빼고 실행한다.

**릴리스로 설치했다면** 한 줄 설치 명령을 `<version>` 자리에 새 버전을 넣어 다시 실행한다.
[macOS에 설치하기](install-macos.md)나 [Windows에 설치하기](install-windows.md)에 있는 명령이다. 압축을 풀기 전에
릴리스의 SHA-256과 대조한다.

그다음 새 터미널을 열고 확인한다.

```sh
oah --version
oah doctor
```

## 2. oah init이 쓴 파일 새로 고치기

`oah`를 업데이트한 뒤 프로젝트마다 실행한다.

| 명령 | 하는 일 |
|---|---|
| `oah rules check` | `AGENTS.md`의 관리 블록이 새 버전의 문구와 다른지 알려 준다. 읽기만 한다. |
| `oah rules sync` | 관리 블록을 새 문구로 바꾼다. 블록 밖의 내용은 그대로 둔다. |
| `oah init` | 계획을 출력한 다음 빠진 것만 쓴다. `--force` 없이는 사용자 파일을 바꾸지 않고, 위키의 색인과 로그는 어떤 경우에도 바꾸지 않는다. |
| `oah models --refresh` | 엔진의 모델 목록을 지금 다시 읽는다. |

이 명령들이 만든 변경은 작은 작업 하나로 따로 커밋한다.

## 3. 스킬: 키트 캐시, 프로젝트 고정본, 전역 폴더

스킬은 세 종류의 자리에 있고 자리마다 업데이트 방식이 다르다. 전체 규칙은 [툴킷](toolkit.md) 문서에 있다.

| 자리 | 업데이트하는 것 | 언제 |
|---|---|---|
| **키트 캐시.** 작업 트리 밖, `oah` 상태 폴더 안에 있다 | `oah skills update` 또는 자동 확인 | `[skills] auto_update = true`이면 `[skills] update_interval_days`(기본 1일)마다 많아야 한 번 자동으로 확인한다. 리스가 열려 있으면 하지 않는다. `oah skills schedule install`은 같은 확인을 매일 돌리는 OS 항목을 더한다. |
| **프로젝트에 추적되는 사본.** `.claude/skills/`와 `.agents/skills/`에 있다 | `oah skills update --project`만 | 사용자가 실행할 때. 이 명령은 그 폴더에 자기 리스를 잡으므로 작업 하나로 따로 실행하고 결과를 커밋한다. |
| **사용자 전역 스킬 폴더** | `oah skills update --global`만 | 사용자가 실행할 때. 먼저 묻는다. 정해진 일정으로 전역 파일을 바꾸는 일은 없다. |

`oah skills update --check`는 받을 수 있는 업데이트를 알려 주기만 하고 아무것도 쓰지 않는다. 업데이트는 반영하기
전에 모두 검사한다. 라이선스가 그대로여야 하고, 스킬마다 제대로 읽혀야 하고, 개인정보 스캔을 통과해야 한다.
실패하거나 수상한 업데이트는 마지막으로 정상이던 커밋으로 되돌리고 알린다. 스킬 하나를 지금 상태로 묶어 두려면
`.oah/config.toml`의 `[skills]` 아래 `pins = ["<name>"]`에 이름을 적는다.

같은 스킬의 사본이 여럿 있으면 프로젝트 사본, 키트 캐시, 사용자와 플러그인 사본 순서로 고른다.

## 4. 게이트 검사기 unlazy

unlazy는 `oah`의 게이트 계약을 검증한 커밋 하나에 고정돼 있고 스킬 업데이트는 이것을 옮기지 않는다. 고정
커밋은 `oah`의 새 버전만 바꿀 수 있다. 고정된 사본은 `oah init --with-unlazy`로 설치한다. `--force`는 맞지 않는
프로젝트 사본을 교체하지 못하며 `.oah/config.toml`을 기본값으로 되돌릴 수 있으므로 더하지 않는다. 사본이 맞지
않는 동안 게이트 명령은 `OAH_UNLAZY_UNPINNED`로 거절한다. 복구는 다음 순서로 한다.

1. `OAH_UNLAZY_DIR`이 설정돼 있으면 새 고정 커밋과 일치하는 사본을 가리키게 하거나 해제한다. macOS에서는
   `unset OAH_UNLAZY_DIR`, PowerShell에서는 `Remove-Item Env:OAH_UNLAZY_DIR -ErrorAction SilentlyContinue`를
   쓴다. 이 변수가 설정돼 있으면 그 위치만 검사하므로 다른 사본을 설치해도 우회되지 않는다.
2. `.agents/skills/unlazy`와 `.claude/skills/unlazy`를 확인한다. 맞지 않는 프로젝트 사본마다 프로젝트 밖의
   백업 위치로 옮기거나 더는 필요 없음을 확인한 뒤 직접 지운다. 첫 경로의 예는 macOS에서
   `mv .agents/skills/unlazy ../unlazy-backup`, PowerShell에서
   `Move-Item .agents\skills\unlazy ..\unlazy-backup`이다. 아직 존재하지 않는 백업 경로를 고른다.
   맞지 않는 프로젝트 디렉터리가 있으면 `--force`를 줘도 init은 `OAH_INIT_UNLAZY_EXISTS`를 보고한다.
3. `oah init --with-unlazy`를 다시 실행하고 계획을 확인한 뒤 고정 사본 설치를 승인한다. 기존 설정은 유지하고
   커밋하기 전에 결과를 살펴본다.

## 5. MCP 템플릿

[`mcp/`](../../mcp/README.md)의 템플릿은 띄우는 서버마다 npm 버전을 고정해 두었다. 프로젝트 안에서 새 버전을
확인한다.

```sh
oah skills update --check
oah skills update
oah doctor
```

두 update 명령은 카탈로그에 고정된 MCP 버전을 npm 레지스트리에 조회한다. `OAH_MCP_UPDATE_AVAILABLE`은 새
버전이 있다는 뜻이며 설치됐다는 뜻은 아니다. `--check`는 알리기만 하고 아무것도 쓰지 않는다. `oah skills update`는
스킬 캐시도 업데이트하며 상태 폴더의 `toolkit/updates.jsonl`에 알림을 기록한다. `oah doctor`는 MCP 항목마다
마지막으로 기록된 알림을 오프라인으로 보여 준다. npm에 조회하거나 내 설정이 업데이트됐음을 증명하지 않는다.
`current`는 더 새 버전을 찾지 못했다는 뜻이다. `unused(offline)`이나 `unused(registry error)`는 확인할 수 없었다는
뜻이므로 나중에 다시 시도한다. 어느 명령도 MCP 고정 버전이나 사용자 MCP 설정을 고치지 않는다.

업그레이드하기로 결정하면 내 설정에 합쳐 넣은 사본의 `@<version>` 부분을 고친 뒤 Claude Code나 Codex를 다시
시작한다. 키트 관리자는 `mcp/` 템플릿과 카탈로그의 고정 버전을 별도로 고친다. 서버마다 출처와 라이선스는
[툴킷](toolkit.md) 문서에 있다.

## 6. 모델

모델은 설정을 고치지 않아도 업데이트를 따라간다.

- Claude의 tier는 Claude Code 자체 별칭(`opus`, `sonnet`, `haiku`)을 쓰고 Claude Code가 이 별칭을 최신 모델로
  풀어 준다.
- Codex의 tier는 내 Codex 설정이 쓰는 모델에서 출발한다. 더 새 모델은 Codex가 기본값으로 표시하거나 내
  `quality_order`에 이름이 있을 때만 쓴다.
- `oah`는 실행할 때마다 먼저 모델 카탈로그가 최신인지 확인한다. 사용자가 tier에 고정한 모델이 늘 이긴다.
- 모델이나 추론 강도가 바뀔 때마다 이전 값과 새 값, 시각, 이유를 기록한다.

`oah models`는 지금 tier마다 쓰는 모델과 추론 강도를 보여 준다.

## 7. docs/UPSTREAM_SYNC.md가 기록하는 것

`oah`는 소유자의 하네스가 나아지는 것을 따라간다. [`docs/UPSTREAM_SYNC.md`](../UPSTREAM_SYNC.md)는 그 하네스에서
마지막으로 검토한 커밋과, 검토한 변경마다 한 줄씩 커밋, 요약, 판정을 기록한다.

- **반영(port).** 일반적인 `oah` 기능으로 새로 작성한다. 그 일을 맡은 계획 항목과 pull request를 함께 적는다.
- **건너뜀(skip).** 개인적이거나 소유자에게만 맞는 변경이다.
- **보류(defer).** 나중에 정한다.

그 하네스에서 복사해 오는 것은 없고 실행할 때 의존하지도 않는다. 사용자는 위 1단계의 일반 `oah` 릴리스로
개선 사항을 받는다.
