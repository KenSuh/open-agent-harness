# 문제 해결

`oah`가 거절할 때는 언제나 `OAH_LEASE_CONFLICT` 같은 코드로 메시지를 시작하고, 나머지 부분에서 다음에 할 일을
알려 준다. 이 문서는 처음 쓰는 사람이 가장 자주 만나는 코드를 단계별로 모았다. 화면에 나온 코드로 찾아본다.

[English](../troubleshooting.md)

테스트(`tests/test_docs.py`)가 이 문서의 코드가 모두 `oah` 소스에 아직 있는지 확인한다. 그래서 이 목록이 이미
사라진 코드를 가리키는 일은 없다.

## 먼저 할 일

- **`oah doctor`를 실행한다.** `oah`가 쓰는 도구를 모두 확인하고 빠진 도구마다 공식 설치 명령을 알려 준다.
- **메시지를 끝까지 읽는다.** 관련된 파일이나 리스, 게이트와 실행할 명령이 들어 있다.
- **종료 코드.** `0`은 완료, `1`은 거절(메시지대로 고치고 다시 실행), `2`는 사용법 오류(옵션이 틀렸거나
  빠졌다. 명령에 `--help`를 붙여 본다)다. 예외로 `oah preflight`는 `ABORT` 판정에도 `2`를 반환한다. 각 `[abort]`
  줄을 고치고 다시 실행한다. `69`는 환경 문제(도구가 없거나 설정 파일이 깨졌거나 저장소가 아닌 폴더)다.
  `75`는 `oah wait`의 제한 시간이 끝나도 실행이 진행 중임을 뜻한다. `oah wait <run-id>`를 다시 실행한다.

## 설치와 설정

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_NOT_A_REPO` | 지금 폴더가 git 저장소 안이 아니다. | 프로젝트 폴더로 `cd`한다. 새 프로젝트라면 먼저 `git init -b main`을 실행한다. |
| `OAH_GIT_MISSING` | git이 없거나 PATH에 없다. | macOS는 `xcode-select --install`, Windows는 `winget install --id Git.Git -e`를 실행하고 새 터미널을 연다. |
| `OAH_CONFIG_INVALID` | `.oah/config.toml`의 값 형식이 틀렸다. 숫자 자리에 글자를 쓴 경우 등이다. | 메시지가 짚은 키를 고치거나 지워서 기본값을 쓴다. |
| `OAH_CONFIG_UNKNOWN_KEY` | `.oah/config.toml`에 `oah`가 모르는 키가 있다. 오타인 경우가 많다. | 지우거나 메시지에 나온 키 가운데 하나로 바꾼다. |
| `OAH_ENGINE_MISSING` | `claude`나 `codex`가 PATH에 없다. | 설치하거나(`oah doctor`가 명령을 알려 준다) `.oah/config.toml`의 `[engines.claude]`나 `[engines.codex]`에 `command`를 적는다. |
| `OAH_UNSUPPORTED_BATCH_SHIM` | Windows에서 엔진이 `oah`가 안전하게 실행할 수 없는 `.cmd`나 `.bat` 파일이다. | 도구의 네이티브 실행 파일을 설치하거나 `.oah/config.toml`에 `command = ["<interpreter>.exe", "<script>"]`를 직접 적는다. |
| `OAH_MODEL_UNRESOLVED` | tier에 쓸 모델을 찾지 못했다. 고정한 모델도, 카탈로그 항목도, 엔진 설정의 기본 모델도 없다. | `.oah/config.toml`에 그 tier의 `model`을 적거나, 엔진에 기본 모델이 생긴 뒤 `oah models --refresh`를 실행한다. |
| `OAH_EFFORT_METADATA_MISSING` | tier가 `highest` 강도를 요청했는데 엔진이 모델의 추론 단계를 알려 주지 않았다. `oah`는 짐작하지 않는다. | 엔진을 한 번 실행해 모델 목록을 저장하게 한 뒤 `oah models --refresh`를 실행한다. 아니면 그 tier에 강도를 직접 적는다. |
| `OAH_EFFORT_UNSUPPORTED` | tier의 강도가 모델이 밝힌 단계에 없다. | 메시지에 나온 단계 가운데 하나나 `highest`로 바꾼다. |
| `OAH_STATE_LOCK_TIMEOUT` | 다른 `oah` 명령이 상태 잠금을 쥐고 있다. | 그 명령이 끝나기를 기다렸다가 다시 실행한다. |

## 리스

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_LEASE_CONFLICT` | 다른 리스가 내 범위와 겹치는 경로를 이미 잡고 있다. 메시지에 그 작업과 에이전트, 워크트리가 나온다. | 그 작업이 끝나기를 기다리거나, 다른 경로를 고르거나, 리스를 잡은 사람에게 내려놓아 달라고 한다. |
| `OAH_SCOPE_EXCEEDED` | `oah finish`가 리스 범위 밖에서 바뀐 파일을 찾았다. | `oah renew --task <task> --agent <agent> --scope <path> …`로 범위를 넓히거나(경로를 모두 다시 적는다) `--allow-out-of-scope`를 준다. 이 선택은 기록된다. |
| `OAH_LEASE_EXPIRED` | 리스 시간(기본 4시간)이 지나 연장할 수 없다. | `oah start`로 새 리스를 잡는다. 만료된 리스도 끝내거나 내려놓을 수는 있다. |
| `OAH_LEASE_NOT_YOURS` | 작업은 맞는데 다른 에이전트 이름을 썼다. | 리스를 잡은 쪽의 `--agent`를 쓴다. 정말 넘겨받으려면 `oah release`에 `--force`를 붙인다. |
| `OAH_WORKER_LEASE_REFUSED` | `oah call`이 띄운 워커가 리스를 바꾸려 했다. 워커는 자신을 띄운 세션의 리스를 쓴다. | 워커 쪽에서 고칠 것은 없다. `oah call`을 실행한 세션이 리스를 끝낸다. |

## 패킷과 의도

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_SCOPE_EMPTY` | 패킷의 `## Scope`에 경로가 없다. 파일 종류로 스킬을 고를 수 없고 `oah start --packet`도 거절한다. | 작업이 쓸 경로를 한 줄에 하나씩 적거나 저장소 전체라면 `- repo`라고 적는다. `oah packet new <task> --scope <path>`로 만들면 자동으로 채워진다. |
| `OAH_PACKET_PLACEHOLDER` | `<checkable condition>` 같은 템플릿 문구가 의도에 남아 있다. | 메시지에 나온 자리표시자를 모두 바꾼다. |
| `OAH_INTENT_NOT_ACCEPTED` | 의도의 `Status`가 아직 `draft`다. 초안은 리스에 묶을 수 없다. | 의도가 원하는 바를 담았으면 그 줄을 `Status: accepted`로 바꾼다. |
| `OAH_INTENT_ID_CHANGED` | `Intent-ID`가 작업 이름이나 앞서 묶은 값과 다르다. | ID는 그대로 둔다. 목표가 바뀌면 `Revision`을 올린다. |
| `OAH_INTENT_NOT_CURRENT` | 의도를 묶은 뒤 패킷이 바뀌어 결속 상태가 `stale`, `missing`, `unbound` 가운데 하나다. | 패킷을 묶었을 때로 되돌리거나, `Revision`을 올리고 승인한 뒤 `oah start --task <task> --agent <agent> --packet <PACKET.md>`로 다시 묶는다. |
| `OAH_INTENT_RETURN_INVALID` | `oah finish --return`에 준 보고가 묶인 의도와 맞지 않는다. | `oah intent show <task>`의 `INTENT` 줄을 그대로 옮기고 성공 조건마다 `AC<n>: met|unmet|deferred — <근거>` 줄을 정확히 하나씩 적는다. |

## 작업 시작

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_PREFLIGHT verdict=ABORT` | 검사 하나가 실패했다. 브랜치가 기본 브랜치보다 뒤처졌거나 다른 리스가 내 경로를 잡고 있다. | `[abort]` 줄을 하나씩 고친 뒤(예를 들어 `git rebase origin/main`) `oah preflight`를 다시 실행한다. |
| `OAH_IMPACT_STALE` | 영향 지도를 새로 만들지 못했다. `oah`는 낡은 지도로 일하지 않는다. | 메시지의 원인을 고치고 `oah impact refresh`를 실행한 뒤 다시 시도한다. |
| `OAH_STAGE_ENGINE_MISMATCH` | 설정상 다른 엔진이 맡은 단계를 이 엔진에게 시켰다. 1차 구현을 Codex에게 시킨 경우 등이다. | 메시지가 말한 엔진으로 실행하거나, `.oah/config.toml`의 `[stages]`를 바꾸거나, `--override-stage`를 준다(기록된다). |
| `OAH_EFFORT_DOWNGRADED` | 엔진이 요청보다 낮은 강도로 실행했다고 보고해서 이 실행은 인정되지 않는다. | 엔진의 설정과 버전을 확인하고 다시 실행한다. `oah`는 강도를 몰래 낮추지 않는다. |

## 게이트

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_UNLAZY_MISSING` | 게이트 검사기 unlazy가 설치돼 있지 않다. | `oah init --with-unlazy`를 실행하거나 메시지가 출력한 `git clone … checkout --detach <commit>` 줄을 실행한다. 그동안에도 `oah gates status`는 구조 보고를 해 준다. |
| `OAH_UNLAZY_UNPINNED` | unlazy 폴더가 있지만 `oah`가 검증한 커밋이 아니다. | 그 폴더를 다른 곳으로 옮긴 뒤 메시지가 출력한 줄로 고정된 사본을 설치한다. |
| `OAH_NODE_MISSING` | unlazy가 쓰는 Node.js를 실행할 수 없다. | <https://nodejs.org>에서 LTS 판을 설치하고 새 터미널을 연다. |
| `OAH_GATES_MISSING` | 작업의 게이트 원장이 없다. | `oah gates new <task>`로 만든다. |
| `OAH_GATES_UNMET` | 통과한 증거가 없는 게이트가 있다. 처음 실행하면 CHECK 명령이 사용자의 승인을 기다린다. | CHECK 명령을 읽어 본 뒤 `oah gates check <task> --approve`를 실행한다. 정말 불가능한 게이트라면 원장에 `ABANDON: G<n> <이유>`를 적는다. |
| `OAH_GATES_UNVERIFIED` | unlazy가 없어서 `oah finish`가 게이트를 검증하지 못한다. | unlazy를 설치하고 `oah gates check <task>`를 실행한다. 아니면 `--allow-structural`로 체크 표시만 보고 끝낸다(검증 안 됨으로 기록된다). |
| `OAH_GATES_SUMMARY_STALE` | `GATES.summary.md`가 게이트 기록과 맞지 않는다. | `oah gates check <task>`를 실행하고 새로 쓴 요약을 커밋한 뒤 끝낸다. |

## 위키

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_WIKI_MAINTENANCE_MISSING` | `oah finish`가 이 작업의 위키 정리 기록을 찾지 못했거나 기록이 낡았다. | 커밋하고 게이트를 돌리고 리뷰를 받기 전에 `oah wiki maintain --task <task>`를 실행한다. `--allow-missing-wiki-maintenance`로 건너뛸 수 있고 이 선택은 기록된다. |
| `OAH_WIKI_RECOVERY_PENDING` | 로그 옮기기(rollover)가 중간에 끊겨서 위키 명령이 짐작하지 않고 멈췄다. | `oah wiki recover`를 실행한 뒤 다시 시도한다. |

## 리뷰

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_REVIEW_DIRTY_TREE` | 추적 중인 파일에 커밋하지 않은 변경이 있다. 리뷰는 커밋을 읽는다. | 커밋하거나 stash한 뒤 `oah review`를 다시 실행한다. |
| `OAH_REVIEW_CHANGES_REQUESTED` | 오류가 아니다. 이번 라운드에서 P0나 P1 문제가 나왔다. | `oah fix --task <task>`를 실행하고 수정을 확인해 커밋한 뒤 `oah fix --task <task> --bind`, 이어서 `oah review --task <task>`를 다시 실행한다. |
| `OAH_REVIEW_NEEDS_DECISION` | 루프가 사용자를 기다리며 멈췄다. 이미 고친 종류의 지적이 다시 나왔거나 고치려면 설계나 저장 형식을 바꿔야 한다. | 결정한 뒤 `oah review --task <task> --decision "<결정한 내용>"`을 실행한다. 결정은 기록된다. |
| `OAH_REVIEW_ROUNDS_EXHAUSTED` | `[review] max_rounds`를 정해 두었고 시리즈가 거기에 닿았다. | `oah review --task <task> --extra-round`를 실행하거나(기록된다) `max_rounds`를 올리거나 지운다. |
| `OAH_REVIEW_ATTEMPTS_EXHAUSTED` | 이번 라운드의 리뷰어 호출이 여러 번 실패해서 `oah`가 유료 리뷰어를 더 부르지 않는다. | 상태 폴더 `runs/`에서 마지막 실행의 `stderr.log`를 읽고 원인을 고친 뒤, 메시지가 출력한 명령에 `--extra-attempt`를 붙여 실행한다. |

`oah fix`는 다음에 할 일을 줄마다 하나씩 출력하고 각 줄은 도달한 단계로 시작한다. 순서대로 따라 한다.

## push와 반영

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_PUSH_REFUSED` | `pre-push` 훅이 이 워크트리에서 끝나지 않은 리스를 찾았다. | `oah finish`로 끝내거나 `oah release --reason`으로 내려놓은 뒤 push한다. |
| `OAH_PUSH_SECRETS` | push하려는 커밋에서 gitleaks가 비밀로 보이는 값을 찾았다. | 그 커밋에서 값을 지우고, 진짜 키라면 발급처에서 바꾼 뒤 push한다. [키](keys.md)를 참고한다. |
| `OAH_LAND_LEASE_OPEN` | 이 워크트리에 아직 열린 리스가 있다. | 그 리스를 `oah finish`로 끝내고 `oah land --check`를 다시 실행한다. |
| `OAH_LAND_REVIEW_REQUIRED` | 지금 head를 승인한 수렴 리뷰가 없다. | 수렴할 때까지 `oah review --task <task>`를 실행한다. 수렴한 뒤 커밋을 더 했다면 라운드가 하나 더 필요하다. |
| `OAH_LAND_SCAN_REQUIRED` | gitleaks가 없어서 반영할 커밋을 검사하지 못했다. | gitleaks를 설치하고(`oah doctor`가 명령을 알려 준다) `oah land --check`를 다시 실행한다. |
| `OAH_LAND_CI_FAILED` | 이 head의 CI 검사가 실패했다. | GitHub에서 그 실행을 열어 원인을 고치고 push한 뒤 CI를 기다렸다가 다시 확인한다. [CI 결과 읽기](ci-starter.md#github-에서-결과-읽기)를 참고한다. |
| `OAH_LAND_CI_UNVERIFIED` | gh가 없거나 결과가 없거나 이 head의 필수 검사 결과가 빠져 기본적으로 반영을 막는다. | gh 설치·로그인 후 워크플로와 작업 브랜치를 push하고 기다렸다 다시 확인한다. 아니면 확인과 반영 명령에 `--accept-unverified-ci "<reason>"`을 붙인다. 예외는 head마다 기록하고 CI는 미검증으로 남아 통과로 세지 않는다. |
| `OAH_LAND_CI_PENDING` | CI가 끝나지 않았거나 결과를 읽지 못했다. | 결과를 기다리고 `gh auth status`와 push한 브랜치를 확인한 뒤 다시 시도한다. CI 예외로 우회할 수 없다. |
| `OAH_LAND_IMPACT_STALE` | 기본 브랜치를 가져오거나 다시 평가하지 못했다. `--offline`을 줬거나 원격이 상대 경로인 경우 등이다. | URL이나 절대 경로로 된 원격을 쓰고 `oah impact refresh`를 실행한 뒤 `oah land --check`를 다시 실행한다. |
| `OAH_LAND_CHECKS_REQUIRED` | 최신 기본 브랜치가 내가 바꾼 파일에 검사를 묶어 두었는데 내 head에서 통과한 증거가 없다. | 같은 CHECK와 EXPECT로 게이트를 원장에 더하고 `oah gates check <task>`를 실행해 커밋한 뒤 다시 확인한다. |

## 그래도 막히면

- `oah status`는 모든 리스와 각 리스가 살아 있는지 만료됐는지 보여 준다.
- `oah intent show <task>`는 의도의 결속 상태를 보여 준다.
- `oah gates status <task>`는 아무것도 실행하지 않고 어떤 게이트를 충족했는지 보여 준다.
- 각 검사가 왜 있는지는 [개념](concepts.md)에 있다.
