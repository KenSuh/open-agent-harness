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
| `OAH_EFFORT_METADATA_MISSING` | tier가 `highest` 강도를 요청했는데 엔진이 모델의 추론 단계를 알려 주지 않았다. `oah`는 짐작하지 않는다. | 엔진을 한 번 실행해 모델 목록을 저장하게 한 뒤 `oah models --refresh`를 실행한다. 아니면 그 tier에 강도를 직접 적는다. |
| `OAH_EFFORT_UNSUPPORTED` | tier의 강도가 모델이 밝힌 단계에 없다. | 메시지에 나온 단계 가운데 하나나 `highest`로 바꾼다. |
| `OAH_STATE_LOCK_TIMEOUT` | 다른 `oah` 명령이 상태 잠금을 쥐고 있다. | 그 명령이 끝나기를 기다렸다가 다시 실행한다. |

## 모델

이 코드들은 `main`에 있고 아직 릴리스하지 않았다. 규칙은 [모델을 고르는 방법](concepts.md#모델을-고르는-방법)에 있고,
`oah models`는 모델별 점수와 빠진 이유를 보여 준다.

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_MODEL_UNRESOLVED` | tier의 `tier_pools`에 CLI가 나열하고 `allowed_models`가 허용하는 모델이 없거나, 허용된 모델 가운데 이 일의 최저 기준에 닿는 모델이 없다. | 그 후보 목록을 고치거나 `.oah/config.toml`의 `allowed_models`에 더 강한 모델을 더한다. CLI가 나열하지 않으면 CLI를 업데이트한다. |
| `OAH_MODEL_CATALOG_UNAVAILABLE` | `oah`에 엔진의 모델 목록이 없어서 아무것도 고를 수 없다. | 엔진을 한 번 실행해 목록을 새로 받은 뒤 `oah models --refresh`를 실행하거나 그 tier에 모델을 고정한다. |
| `OAH_MODEL_NO_SCORES` | 벤치마크 점수로 모델을 고를 수 없다. 패키지 자료가 손상됐거나 나열된 모델에 쓸 수 있는 점수가 없다. | `oah models`로 이유를 보고 `oah`를 다시 설치하거나 그 tier에 모델을 고정한다. |
| `OAH_MODEL_NOT_LISTED` | 새로 읽은 Codex 카탈로그에 이 tier에 고정한 모델이 없다. 아무것도 실행하지 않았다. | `oah models`를 실행하고 `.oah/config.toml`의 고정 모델을 바꾼다. |
| `OAH_MODEL_BELOW_FLOOR` | 리뷰의 tier, 모델, 강도가 최저 기준 아래이거나 허용된 모델 가운데 그 기준에 닿는 모델이 없다. 라운드와 시도는 쓰지 않았다. | 메시지가 말한 tier를 쓰거나 `.oah/config.toml`의 고정 모델, 후보 목록, `quality_order`, `allowed_models`를 바꾼다. |
| `OAH_MODEL_FLOOR_UNCHECKED` | 알림이다. 직접 적은 모델에 점수가 없어서 리뷰의 최저 기준을 확인하지 못했다. 영수증에 그렇게 남는다. | 확인을 원하면 점수가 있는 모델을 적는다. |
| `OAH_MODEL_SUPERSEDED` | 알림이다. 직접 적은 모델에 더 새 세대가 있다. 그대로 실행한다. | 원할 때 `.oah/config.toml`의 그 줄을 바꾼다. |
| `OAH_MODEL_UNSCORED` | 알림이다. CLI가 나열한 모델에 아직 쓸 수 있는 점수가 없어서 옛 모델이 남는다. | 할 일은 없다. 점수가 나오면 합류한다. 지금 쓰려면 `.oah/config.toml`에 이름을 적는다. |
| `OAH_MODEL_UNKNOWN_FAMILY` | 알림이다. CLI가 나열한 모델의 모델군을 `oah`가 몰라서 자동으로 고르지 않는다. | `quality_order`나 tier에 이름을 적거나 `oah`를 업데이트한다. |
| `OAH_MODEL_CHANGED` | 알림이다. 카탈로그 재구성으로 어떤 단계에서 규칙이 쓰는 모델이 바뀌었다. 한 번 나오고 `changes.jsonl`에 남는다. | 할 일은 없다. 모델을 지키려면 고정한다. |
| `OAH_MODEL_ORDER_IGNORED` | `oah models`의 알림이다. `quality_order`가 옛 기본값이라 설정하지 않은 것으로 치고 점수가 고른다. | 그 줄을 지우거나 다른 모델을 적어 직접 순서를 정한다. |
| `OAH_SCORES_REFRESH_FAILED` | 알림이다. 카탈로그를 다시 만들 때 새 점수를 받지 못해서 마지막으로 성공한 사본이나 `oah`와 함께 배포된 사본이 골랐다. 이유는 메시지에 있다. 멈춘 것은 없다. | 잠깐의 네트워크 문제라면 할 일이 없다. `oah`가 다음 재구성 때 다시 받는다. 키가 거절됐다면 `ARTIFICIAL_ANALYSIS_API_KEY`를 다시 저장한다. 요청을 멈추려면 `[models] fetch_scores = false`로 둔다. |
| `OAH_SCORES_AA_UNUSED` | 알림이다. Artificial Analysis 사본에 CLI가 나열한 모델의 점수가 없어서 그 엔진은 Epoch AI 점수로 골랐다. | 할 일이 없다. Artificial Analysis가 그 모델에 점수를 매기면 사라진다. |
| `OAH_SCORES_FILE_INVALID` | 알림이다. `oah` 상태 폴더의 점수 사본이 손상되었거나 바뀌어서 없는 것으로 쳤다. | 메시지가 말한 파일을 지우거나 `oah models --refresh`로 다시 받는다. |

## Jev와 난이도

이 코드들은 `main`에 있고 아직 릴리스하지 않았다. Jev가 무엇을 받고 그 tier를 언제 쓰는지는
[Jev가 난이도를 판정한다](concepts.md#jev가-난이도를-판정한다)에 있다.

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_RERANK_SCOPE_WIDENED` | 프로젝트마다 한 번 나오는 알림이다. 이제 Jev가 난이도를 판정하려고 작업의 Intent 글과 Scope 경로를 최대 16,000자까지 받는다. | 할 일이 없다. Jev 요청을 모두 멈추려면 `.oah/config.toml`의 `[rerank]`에 `enabled = false`를 둔다. |
| `OAH_RERANK_SECRET` | 메시지가 말한 부분(Intent 글, Scope 경로, 요청 글)에 키나 개인 경로가 있다고 개인정보 규칙이 판단해서 Jev에게 묻지 않고 규칙 tier로 실행했다. 보낸 것은 없다. | 패킷이나 요청 파일에서 그 글을 지우거나, 규칙 tier를 그대로 쓴다. |
| `OAH_DESIGN_UNJUDGED` | `oah packet new`의 경고다. 이 작업에 기록된 설계 판정이 없다. 패킷은 그래도 만들었다. | 설계를 쓰기 전에 메시지가 말한 명령 `oah recommend --kind design --task <task> --request-file <file>`을 실행한다. |
| `OAH_RECOMMEND_USAGE` | `oah recommend` 인자가 서로 맞지 않는다. `--kind design`이나 `packet` 없이, 또는 올바른 `--task` 없이 `--request-file`을 줬거나, `--request-file` 없이 `--task`나 `--scope`를 줬거나, 축이 빠졌다. | 메시지가 보여 주는 명령이나 `oah recommend --help`를 따른다. |
| `OAH_RECOMMEND_REQUEST_TOO_LARGE` | 요청 파일이 1 MB를 넘는다. 보내거나 기록한 것은 없다. | 요청 자체만 더 작은 파일에 쓴다. Jev는 최대 16,000자만 읽는다. |
| `OAH_RECOMMEND_REQUEST_UNREADABLE` | 요청 파일이 없거나, 파일이 아니거나, 읽을 수 없거나, UTF-8이 아니다. 보내거나 기록한 것은 없다. | 요청을 UTF-8 텍스트 파일로 저장하고 그 경로를 준다. |
| `OAH_AGENT_FILE_EDITED` | `oah doctor`의 알림이다. `.claude/agents/oah-<tier>.md` 파일이 `oah`가 만드는 내용과 달라서 그대로 두며, 그 모델은 더 이상 최신으로 유지하지 않는다. | 그대로 쓰거나, 지우고 `oah init --with-agents`를 실행해 `oah`가 다시 쓰게 한다. |
| `OAH_AGENT_FILE_STALE` | `oah doctor`의 알림이다. `oah`가 만든 하위 에이전트 파일의 모델이나 추론 강도가 지금의 선택과 다르고, 이를 고칠 세션 시작 훅이 없다. | `oah hooks install session-start`로 훅을 설치하거나, 파일을 지우고 `oah init --with-agents`를 실행한다. |

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

## 도구 업데이트

이 코드들은 `main`에 있고 아직 릴리스하지 않았다. 패스는
[작업이 끝난 뒤 도구 업데이트](concepts.md#작업이-끝난-뒤-도구-업데이트)에서 설명한다.

| 코드 | 뜻 | 다음에 할 일 |
|---|---|---|
| `OAH_UPDATE_STARTED` | `oah finish`가 도구 업데이트 패스를 백그라운드로 시작했고 기다리지 않았다. | 패스가 끝나면 `oah update status`가 결과를 보여 준다. |
| `OAH_UPDATE_SKIPPED` | 패스가 돌지 않았다. `[updates]`의 `after_finish`가 꺼져 있거나 읽을 수 없거나, 직전 패스가 `interval_hours` 안에 시작했거나, 호출한 쪽이 발주된 워커이거나, 백그라운드 시작이 실패한 경우다. | 줄에 적힌 이유를 읽는다. 켜려면 `.oah/config.toml`의 `[updates]`에 `after_finish = true`를 두고, 지금 돌리려면 `oah update`를 실행한다. |
| `OAH_UPDATE_BUSY` | 이 컴퓨터의 다른 패스가 `~/.oah/updates/pass.lock`을 잡고 있다. | 기다렸다가 `oah update status`를 실행한다. `oah update`는 이 코드와 함께 1로 끝난다. |
| `OAH_UPDATE_DEFERRED` | 리스가 살아 있거나 run이 진행 중이어서(또는 run 저장소가 연결되거나 잘못되거나 읽을 수 없어서) 아무것도 갱신하거나 기록하지 않았다. | 작업을 마친다. 진행 중인 것을 남기지 않는 다음 `oah finish`가 패스를 시작하고, 그때 `oah update`를 실행해도 된다. `running`에 멈춘 run은 계속 미루게 하므로 `oah wait <run-id>`로 확인한다. |
| `OAH_UPDATE_STATE_UNAVAILABLE` | 이력은 있는데 `~/.oah/updates/state.json`이 없거나, 상태·이력 기록을 읽을 수 없거나 내용이 잘못돼 모든 도구를 다시 확인한다. 잘못된 이력 줄은 건너뛰며 `oah update status`가 그 수를 알린다. | 망가진 파일을 지우고 `oah update`를 실행한다. 패스가 새 기록을 쓴다. 두 기록이 모두 아직 없으면 신규 설치로 본다. |
| `OAH_UPDATE_FINISHED` | 패스가 끝났다. `complete`는 Claude 마켓플레이스를 포함한 모든 고정 구성요소에 updated, current, unsupported 결과가 있다는 뜻이다. 빠진 결과는 unverified로 세어 `needs-attention`이 된다. 시간 제한(키트 fetch는 `timeout_seconds`, 나머지 키트 Git 명령는 600초, gitleaks는 300초)을 넘긴 명령은 멈추고 `failed` 및 «timed out; the tool may be partly updated»로 기록한다. | 그 위 `oah:` 줄마다 적힌 다음 단계를 따른다. 시간 초과라면 줄에 나온 도구 명령을 직접 실행해 설치 상태를 확인한다. `oah update status`가 다시 보여 준다. |
| `OAH_UPDATE_OFF` | `oah doctor` 알림이다. `.oah/config.toml`의 `[updates]`에 `after_finish`가 없어서 도구 업데이트가 꺼져 있다. | 켜려면 `after_finish = true`를 두고(그다음 `oah update`가 지금 패스를 돌린다), 계속 끄고 알림을 없애려면 `false`를 둔다. |
| `OAH_UPDATE_UNSETTLED` | `oah doctor` 알림이다. 마지막 패스가 `updated`나 `current`가 아닌 결과를 남겼다. | 그 줄의 다음 단계를 따른 뒤 `oah update`를 실행한다. |

## 그래도 막히면

- `oah status`는 모든 리스와 각 리스가 살아 있는지 만료됐는지 보여 준다.
- `oah intent show <task>`는 의도의 결속 상태를 보여 준다.
- `oah gates status <task>`는 아무것도 실행하지 않고 어떤 게이트를 충족했는지 보여 준다.
- 각 검사가 왜 있는지는 [개념](concepts.md)에 있다.
