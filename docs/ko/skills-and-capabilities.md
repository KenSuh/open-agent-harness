# 스킬과 능력

설계는 무엇을 만들지 말한다. 워커가 일을 시작하기 전에 `oah`는 설계로 세 가지를 더 한다. 아홉 설계 단계를
추적하고, 단계마다 쓸 전문 스킬을 찾아 검토하고, 검토한 스킬 본문과 그 스킬이 이름 붙인 도구를 워커에게
넘긴다. 이 문서의 모든 일은 이 컴퓨터 안에서 돌고 같은 입력에는 같은 결과를 낸다. 예외는 무엇을 보낸다고
밝힌 검색 한 단계뿐이다.

[English](../skills-and-capabilities.md)

## 아홉 설계 단계

설계는 다른 부분 옆에 `coverage` 부분을 둔다. 여기에 단계마다 그 단계가 적용되는지, 각 확인 항목의 상태,
연결된 스킬, 남겨 둔 공백을 기록한다.

| 단계 | 필수 확인 항목 | 해당 없음으로 둘 수 있나 |
|---|---|---|
| `purpose` | users, outcome, first-scope, terms, open-decisions | 아니오 |
| `behavior` | normal-flow, failure-flow, cancel-flow, resume-flow, user-result | 아니오 |
| `data` | entities, ownership, lifetime, integrity | 아니오 |
| `access` | subjects, rules, cross-owner-counterexample, enforcement-point | 예 |
| `server` | responsibilities, api-contract, secrets, external-effects | 예 |
| `payment` | state-transitions, failure-refund-cancel, duplicate-out-of-order-missing, server-confirmation, reconciliation-owner | 예 |
| `quality` | platforms, cost-performance, observability, deploy-rollback, recovery-scenario, 그리고 웹·모바일·데스크톱 화면이 있으면 accessibility | 아니오 |
| `materials` | stack-options, sources-licenses, adoption, unverified, first-scope-done, module-boundaries | 아니오 |
| `confirm` | intent-alignment, first-implementation-scope, completion-criteria, packet-ready | 아니오 |

- `oah design coverage P D`는 단계마다 상태, 다시 확인할 것, 다음에 물을 단계를 보여 준다. 읽기만 한다.
  `--accepted`나 `--revision N`은 다른 판을 읽는다. `--check`는 설계가 완료될 때까지
  `OAH_COVERAGE_INCOMPLETE`와 함께 1로 끝난다.
- `oah design coverage edit P D --from EDITS.json`은 작업 목록으로 새 coverage 부분을 만들고, 그것을 저장할
  정확한 `oah design save` 명령과 함께 출력한다. 스스로는 아무것도 쓰지 않는다. 작업은 `set-applicability`,
  `set-check`, `defer-stage`, `confirm-stage`, `set-tech`, `link-skill`, `unlink-skill`, `add-gap`,
  `clear-recheck`이다. 파일에는 그것을 쓸 때 기준으로 삼은 판과 digest를 적는다. 그래서 옛 판을 기준으로 만든
  편집은 거절된다.
- 결정이 바뀌면 그 결정이 닿는 단계만 다시 확인하면 된다. 이를 위한 표시를 저장하지 않는다. 읽을 때마다 상태를
  새로 계산한다.

**coverage 부분의 원본은 하나다.** 사람이 읽는 절은 JSON 블록에서 만들어지고, 그 절을 손으로 고치면
`OAH_COVERAGE_EDITED`로 거절된다. 사용자가 치르는 비용은 이렇다. coverage는 텍스트 편집기가 아니라
`coverage edit`과 `oah design save`로 바꾼다.

**아홉 단계가 모두 끝나야 설계가 완료된다.** 적용되는 단계는 필수 확인 항목에 모두 답한 뒤 사용자의
확인(`confirm-stage`)을 받아야 한다. 제안이나 미룸은 영향이 작을 때만 답으로 센다. 적용되지 않는 단계는 이유와
함께 해당 없음으로 표시한다. 항목 하나에 답했다고 설계가 완료되지는 않는다. 사용자가 치르는 비용은 이렇다.
`--check`를 통과하기 전에 질문에 더 답하고 단계마다 한 번씩 확인한다.

## 스킬 찾기와 검토

필요(need)는 전문 스킬이 도울 수 있는 단계 확인 항목이다. `oah skills find --design P/D`는 설계의 필요에 맞는
스킬을 찾는다. `--design-stage`, `--check`, `--tech LABEL=VALUE`로 범위를 좁힌다.

- 제공자는 `seed`(작은 내장 목록), `installed`(이 컴퓨터에 이미 있는 스킬), `skills-sh`(공개 skills.sh 검색)다.
  무엇을 보내는 것은 `skills-sh`뿐이고, 처음 쓸 때 안내를 보여 준다.
- `oah skills review`는 스킬 폴더 하나를 커밋에 고정하고 라이선스, front matter, 지원하는 호스트, 지시처럼 보이는
  줄의 선별 결과를 기록한다. 아무것도 설치하지 않는다. 폴더는 `--hit QUERY_ID:N`(저장된 검색의 결과),
  `--seed ID`, `--github OWNER/REPO --path PATH` 중 하나로 고른다. `--update REVIEW_ID`는 검토한 출처를 다시
  확인한다.
- `oah skills candidates --design P/D`는 저장된 검토와 검색 결과를 현재 결정과 맞춰 보여 주고,
  `oah skills read --review ID`는 검토한 본문을 데이터로 감싸 출력한다. 둘 다 아무것도 보내지 않는다.
- 발주된 워커는 검색, 검토, 검토 읽기를 할 수 없다(`OAH_SKILLS_WORKER_REFUSED`). 리드만 한다.
- 검색에는 작업이 필요 없다. `--design`을 주면 보고서의 `operation`이 읽은 설계의 판과 digest를 적는다. 승인되지
  않은 초안이어도 마찬가지다. `--design`이 없으면 operation은 설계를 적지 않는다. 어느 쪽도 워커에게 아무것도
  넘길 수 없다. 넘기는 일은 작업 자신의 발주에서만 일어난다.

`.oah/config.toml`의 `[skills.discovery]`에는 다음 키가 있다.

| 키 | 기본값 | 뜻 |
|---|---|---|
| `enabled` | `true` | `false`면 검색 요청을 하나도 보내지 않는다. |
| `provider` | `"skills-sh"` | 검색 제공자다. |
| `cache_hours` | `24` | 성공한 검색을 다시 쓰는 시간이다. |
| `max_queries` | `4` | `find` 한 번에 보내는 검색 요청 수다. 나머지는 미룬다. |
| `license_allow` | `[]` | 검토가 받아들이는 라이선스다. 비어 있으면 `[sources]` 목록을 쓰고, 그 기본값은 MIT다. |

**공개된 낱말만 컴퓨터 밖으로 나간다.** 검색어는 oah가 정해 둔 단계, 확인 항목, 기술 이름으로 만든다. 정하지
않은 기술, `none`, `other:…` 같은 로컬 값은 아무것도 보내지 않는다. 직접 고른 낱말은 `--public-query`로 줄 수
있지만, 그 낱말을 쓰는 공개 https 페이지(`--public-source`)와 `--need`를 함께 줘야 한다. 사용자가 치르는 비용은
이렇다. oah에 공개 이름이 없는 기술은 공개 검색어를 줄 때까지 검색되지 않는다.

**바깥 글은 데이터다.** 스킬 본문은 `oah`에게 지시하지 않고, 검토는 찾은 것을 기록할 뿐이다. 사용자가 치르는
비용은 이렇다. 찾은 스킬은 검토하고 `coverage edit`으로 연결하기 전까지 아무 일도 하지 않는다.

## 워커에게 스킬 본문 넘기기

`oah call`은 run을 준비할 때 무엇이든 쓰기 전에 스킬부터 받아들인다. 예산 확인, 판정, 라운드, 예약보다
먼저다. 작업이 따르는 승인된 coverage의 연결 중에서 이번 발주 단계(`initial`, `correction`, `review`)를 가리키는
것이 run의 스킬 묶음이 된다.

- 항목마다 검토한 본문이나 그와 일치하는 설치본을 쓰고, 줄바꿈을 맞춘 SHA-256으로 검토의 고정값과 대조한다.
  필수 보조 파일은 스킬과 함께 넘긴다.
- 브리프에는 표시된 블록 하나(`<!-- OAH_SKILL_TEXTS:BEGIN -->`부터 `<!-- OAH_SKILL_TEXTS:END -->`까지)가 설계
  블록 뒤에 들어간다. run 폴더의 `skills.json`이 묶음을 기록한다.
- 엔진을 시작하기 직전에 oah는 설치본의 해시를 다시 잰다. 그사이 바뀐 설치본은
  `OAH_SKILL_NOT_DELIVERABLE(changed)`로 거절된다.
- `oah skills plan --task T --stage S [--engine claude|codex]`는 같은 받아들임을 실행해 다음 발주가 무엇을 담을지,
  아니면 왜 거절될지 출력한다. 아무것도 쓰거나 보내지 않는다.
- `oah skills read --run RUN --item N`은 묶음 항목 하나를 출력하고 읽었다는 기록을 남긴다. 워커는 자기 run의
  묶음만 읽을 수 있다.
- coverage에 연결된 스킬이 없는 작업은 이전과 똑같은 브리프를 받는다.

**받아들임은 한 번, 무엇이든 쓰기 전에 한다.** 넘길 수 없는 필수 스킬이 있으면 호출은 정해진 코드로 거절된다.
`OAH_SKILL_MANDATORY_MISSING`, `OAH_SKILL_REFERENCE_UNAVAILABLE`(`inaccessible` 또는 `unsupported-engine`), 또는
이유(`user-only`, `license`, `host`,
`marker`, `pin-mismatch`)가 붙은 `OAH_SKILL_NOT_DELIVERABLE`이다. 다시 시도하지도, 다른 길로 돌아가지도 않고,
run 폴더도 만들지 않는다. 넘길 수 없는 선택 스킬은 건너뛰고 목록에 남긴다. 사용자가 치르는 비용은 이렇다.
검토가 없거나 고정값이 어긋나면 다시 검토하거나 스킬 연결을 끊을 때까지 발주가 막힌다.

**바이트 예산은 고정돼 있다.** 스킬 본문은 196,608바이트까지, 브리프 전체는 524,288바이트까지, 항목은 128개까지다.
예산을 넘으면 호출은 `OAH_SKILLS_BUDGET_EXCEEDED`로 거절되고 스킬마다 크기를 보여 준다. 그것을 보고 작업을
나누거나 선택 스킬의 연결을 끊는다. 사용자가 치르는 비용은 이렇다. 스킬이 많으면 작업을 여러 개로 나눠야 한다.

**큰 파일은 참조로 넘긴다.** 스킬 본문이 스킬 예산에 들어가지 않으면, oah는 스킬의 보조 파일을 큰 것부터 브리프
밖으로 빼고 그 자리에 "Read `<경로>` in full with your Read tool before you start"라는 줄과 줄 수, digest를
적는다. 스킬 본문 자체는 언제나 브리프 안에 남는다. 이 방식은 Claude 프로세스 run(`oah call claude`를 앞에서,
백그라운드에서, 또는 Orca 워커로 실행)에서만 쓰고, 작업 디렉터리 안에 고정된 바이트 그대로인 스킬 설치본이
있어야 한다. 예를 들면 `.claude/skills/<이름>/` 아래다. 그 run에서만 oah는 Claude에
`--output-format stream-json --verbose`를 요청하고(다른 인자는 바꾸지 않는다), 스트림의 Read마다 파일 바이트와
맞춰 보고, run 폴더에 `skills-receiving.json`을 쓴다. 필수 파일을 모두 끝까지 읽었으면 `reference_delivery: complete`, 아니면
`incomplete`다. 읽지 않은 파일이 있어도 run의 결과는 그대로 두고 경고를 남기며, 기록은 전달이 끝나지 않았다고
적는다. result 이벤트가 없는 스트림은 `OAH_ENGINE_FAILED`로, 읽기를 기록하지 못하면 `OAH_SKILL_RECEIVING_FAILED`로
run을 실패 처리한다. Codex, 네이티브 서브에이전트, `oah fix` 수정 run은 oah에 읽기를 보여 주지 않으므로, 그곳에서는
브리프에 들어가지 않는 필수 파일이 `OAH_SKILL_REFERENCE_UNAVAILABLE(unsupported-engine)`으로 거절된다. 작업
디렉터리 밖의 설치본은 `OAH_SKILL_REFERENCE_UNAVAILABLE(inaccessible)`로 거절된다. 엔진을 시작하기 직전에 oah는
참조한 파일의 해시를 다시 잰다. 사용자가 치르는 비용은 이렇다. 그런 스킬은 저장소 안에 설치해야 하고 Claude 프로세스
run에서만 돌며, 모델은 파일마다 끝까지 읽느라 컨텍스트를 쓴다.

**사람이 직접 부르는 스킬은 사람에게 남는다.** 사람만 부를 수 있는 스킬은 넘기지 않는다. 그런 스킬이 필수면
`OAH_SKILL_NOT_DELIVERABLE(user-only)`로 거절된다. 사용자가 치르는 비용은 이렇다. 그런 스킬은 직접 실행한다.

## 작업과 하위 작업의 스킬

`oah call`은 디스패치 단계마다 작업의 스킬 필요도 정한다. 설계가 없으면 작업 의도에 대한 필요 하나, 설계가 있으면
수락된 coverage가 스킬을 요구하는 단계 점검마다 하나다. 필요에는 요구를 적은 패킷 바이트(본문이 아니라 범위와
digest), 하위 작업, 공개 기술 단어, 플랫폼, 사용자 로컬 제약의 digest, 탐색 설정이 들어간다. 무엇이든 바뀌면 다른
필요가 된다.

- `oah skills find --task T`는 시작한 작업의 필요를 그 작업 자신의 승인 아래에서 `max_queries` 안으로 찾는다.
  `--subtask ID`, `--tech LABEL=VALUE`, `--constraint TEXT`(예: 버전), `--required`를 주면 필요 하나를 직접
  적는다. 설계가 있으면 `--design-stage`와 `--check`로 점검을 고른다. `--dispatch-stage`는 `initial`(기본),
  `correction`, `review` 중 하나다. `--out FILE`은 필요 파일을 저장하고, `--needs FILE`은 저장한 파일의 필요를
  다시 찾는다.
- `oah skills select --needs FILE [--need ID] --review ID ...`는 설계 없는 작업의 필요 하나에 검토한 스킬을
  묶는다. 리드만 고른다. 선택은 `<state>/skills/task-selections/` 아래에 한 번 저장되고, 만든 lease 아래에서만
  쓰인다.
- `oah call ... --skill-needs FILE`과 `oah skills plan ... --skill-needs FILE`은 그 파일의 필요를 쓴다. oah는
  각 필요를 지금의 패킷, 범위, 종류, 위험도, 플랫폼, 설정과 다시 맞춰 보고, 더는 맞지 않는 필요는
  `OAH_SKILL_NEEDS_INVALID(stale)`로 거절한다(다른 이유는 `malformed`, `foreign`, `scope`, `design-bound`).
- 고른 스킬은 링크된 스킬처럼 전달되고, 브리프의 줄은 `For task need …`로 적힌다. 선택이 없는 필수 필요는 무엇도
  쓰기 전에 `OAH_SKILL_MANDATORY_MISSING`으로 거절된다. 엔진이 시작하기 직전에 oah는 그 선택이 아직 리드의
  현재 선택인지 확인하고, 아니면 `OAH_SKILL_NOT_DELIVERABLE(changed)`로 거절한다.
- 승인 뒤에 호출은 기술이나 설계 점검을 적은 필요마다 직접 찾아보고 작업 탐색 기록을 저장한다.
  `skills.json`은 그것을 `task_discovery`에 적는다. 검토한 결과는 사용자가 고를 때까지, 설계가 있으면 coverage에
  링크하고 설계를 수락할 때까지 제안일 뿐이다. `OAH_SKILL_DISCOVERY`로 시작하는 줄이 제안이 기다릴 때, 링크한
  스킬 아래의 기술 결정이 바뀌었을 때, 검색이 실패했을 때를 알려 준다. 사용자만 쓰는 기술 값은 보내지 않고, 그
  기록에 `public-term-required` 공백이 남는다. 기술도 설계 점검도 적지 않은 필요는 저장하지 않는다.

**설계가 있는 작업은 설계를 따른다.** 그런 작업에서 `oah skills select`는
`OAH_SKILL_NEEDS_INVALID(design-bound)`로 거절되고, 수락된 coverage가 링크하지 않은 필수 필요는 호출을
거절한다. 사용자가 치르는 비용은 이렇다. 그런 작업에 새 스킬을 쓰려면 coverage 링크와 설계 수락이 한 번 더 필요하다.

**선택은 필요 하나와 lease 하나에 속한다.** 같은 기술을 쓰는 두 하위 작업은 필요도 선택도 다르고, 버전 제약이
다르면 다른 필요다. 작업을 다시 시작하면 예전 선택은 더는 쓰이지 않는다. 사용자가 치르는 비용은 이렇다. 바뀐 필요나 새
lease에서는 다시 고른다.

**호출이 검색할 수 있다.** 필요에 공개 기술 단어가 있는 호출은 승인 뒤에 skills.sh로 최대 `max_queries`번
검색하며, 한 번에 15초 제한이 있고 실행 잠금 아래에서는 하지 않는다. 답과 바뀌지 않은 필요의 탐색 결과는
`cache_hours` 동안 다시 쓴다. `[skills.discovery] enabled = false`로 끈다. 사용자가 치르는 비용은 이렇다. 그런 호출 중
그날 첫 호출은 검색을 기다릴 수 있다. `oah fix` 수정은 검색하지 않고 작업 선택도 담지 않는다.

## 네이티브 서브에이전트

`oah call ENGINE --stage S --task T --launcher native`는 별도 프로세스를 띄우지 않고, 지금 함께 일하는 AI에게
브리프를 넘긴다. 그 AI(리드)가 자기 서브에이전트에게 준다. Claude Code의 Agent 도구나 Codex의 네이티브
서브에이전트다.

- 다른 `oah call`과 같은 확인과 스킬 받아들임을 거친 뒤 run 폴더 하나에 브리프, `skills.json`,
  `native-offer.json`을 쓰고, 정확한 메시지를 바이트 수와 SHA-256과 함께 출력한다. Claude에는 Agent 도구 인자 세
  개를 출력한다. `subagent_type`(`oah init`이 쓰는 `oah-<tier>` 에이전트), `description`, `prompt`다. Codex에는
  메시지만 출력한다.
- 유료 판정을 묻지 않고, 아무것도 예약하지 않고, 라운드를 세지 않고, 엔진을 시작하지 않는다. run은 `prepared`로
  남고, `oah wait`도 Orca 워커도 그 run을 시작하지 않는다.
- 브리프 첫 줄은 서브에이전트에게 별도 oah 프로세스가 아닌 네이티브 서브에이전트라고 알리고, 답의 끝에
  `oah-canary:` 줄을 붙이라고 한다. 서브에이전트는 `oah skills read --run R --item N`으로 스킬 본문을 다시 읽을 수
  있고, 그 읽기는 기록된다.
- 서브에이전트가 돌아오면 리드는 그 답을 파일로 저장하고 `oah call settle --run R --offer-digest H
  --host-child-id ID --outcome succeeded|failed --result-file FILE [--canary VALUE]`를 실행한다. 정산은 작업, 의도,
  패킷, 범위, 리스, 설계, 스킬 본문, 호스트 대상을 다시 확인한다. 바뀐 것이 있으면 `OAH_NATIVE_OFFER_STALE`로
  거절하고 옛 제안은 증거로 남긴다. 제안은 24시간 안에 정산할 수 있다.
- 정산은 `native-settlement.json`과 `skills-receiving.json`을 쓴다. 같은 값으로 다시 실행하면 처음 영수증을
  돌려주고, 다른 값이면 `OAH_NATIVE_SETTLE_CONFLICT`로 거절한다.

**제안은 전달이 아니고, 정산은 적용이 아니다.** 호스트는 oah에 서명된 영수증을 주지 않으므로 전달은
`parent-attested`로, 증거는 `cooperative`로 기록한다. 카나리가 맞아도 그 토큰이 돌아왔다는 것만 증명한다. 스킬
본문은 `oah skills read`가 읽기를 기록했을 때만 읽은 것으로 치고, 스킬을 적용했다고 표시하는 것은 없다. 정산은
리뷰나 프로세스 성공으로 세지 않고, 작업을 끝내지도 리스를 풀지도 않는다. 사용자가 치르는 비용은 이렇다.
네이티브 서브에이전트가 돌아올 때마다 `oah call settle` 명령을 하나 더 실행한다.

**네이티브는 플래그일 뿐 기본값이 되지 않는다.** `--launcher native`로만 고르고, `[call] launcher = "native"`는
`OAH_CONFIG_INVALID`로 거절된다. 네이티브 서브에이전트는 리드의 환경을 함께 쓰므로 oah의 워커 보호는 협조에
기댄다. oah는 그것이 리드 전용 명령을 실행하는 것을 막지 못한다. 사용자가 치르는 비용은 이렇다. 기존 설정에는
없고, 네이티브는 호출마다 고른다.

**돌려받은 결과는 데이터다.** 결과 파일은 실행하지 않는다. UTF-8이어야 하고 65,536바이트 이하여야 하며
`<!-- oah`로 시작하는 줄이 없어야 한다. 아니면 정산은 `OAH_NATIVE_RESULT_INVALID`로 거절된다. 사용자가 치르는
비용은 이렇다. 긴 로그가 아니라 짧은 답을 저장한다.

Claude 제안에는 단 에이전트 파일이 있어야 한다. 없으면 `OAH_SKILL_HOST_UNSUPPORTED`로 거절된다. 브리프에 다 들어가지
않는 큰 설계도 이 경로에서는 거절된다. 서브에이전트의 읽기를 지켜볼 수 없기 때문이다.

## 스킬이 이름 붙인 능력

넘겨진 스킬은 `allowed-tools` 줄에 MCP 서버의 도구 같은 도구 이름을 적을 수 있다. `oah`는 이름 붙은 도구마다
능력 필요를 만들고 경로를 정한다.

- `oah capabilities show [--engine claude|codex]`는 자식 엔진에 설정된 것을 보여 준다. Claude Code와 Codex 설정
  파일에서 읽은 이름과 켜짐, 승인 같은 표시뿐이다. 연결됐다고 말하지 않고, 키나 URL 같은 값을 옮기지 않는다.
- `oah capabilities plan --task T --stage S`는 작업의 필요마다 경로를 정하고 아무것도 쓰지 않는다. 필요는 다음 중
  한 경로를 받는다.

| 경로 | 언제 |
|---|---|
| `direct-unverified` | 자식에 도구가 설정돼 있지만 스키마, 연결, 승인을 모른다. 선택 필요에만 쓸 수 있다. |
| `parent-relay` | 리드에게 도구가 있다. 리드가 실행해 결과를 기록하고, 워커는 그 결과를 데이터로 받는다. |
| `unsupported` | 쓸 수 있는 경로가 없다. 필수 필요면 거절된다. |

- 중계는 세 단계다. `plan --parent-inventory FILE`이 리드가 채울 수 있는 필수 필요마다 중계 요청을 출력한다.
  리드가 도구를 직접 실행하고 `oah capabilities relay --request FILE --capability ID --result-file FILE
  --invocation FILE`로 결과를 저장한다. 그다음 `oah call ... --parent-inventory FILE --relay-result HASH`가
  스킬 본문 뒤의 `## Capabilities` 블록에 결과를 넣어 넘긴다.
- `.oah/config.toml`의 `[capabilities]`에는 서버에 oah의 능력 분류 하나를 붙이는 `classes = {server = "class"}`와
  능력 사이 의존을 더하는 `requires = {"<id>" = ["<id>", ...]}`가 있다. 플러그인의 서버는 그 플러그인이 켜져
  있어야 한다. 의존은 최대 세 번에 걸쳐 풀고, 순환이 있으면 찾은 고리를 적어 거절한다.

**설정에 적힌 이름은 연결이 아니다.** 필수 필요는 새로 받은 중계로만 채운다. 설정에 이름만 있는 도구는
`OAH_CAPABILITY_UNSUPPORTED(unknown-schema)`나 `(pending-approval)`로 거절된다. 사용자가 치르는 비용은 이렇다.
필수 도구라면 발주 전에 리드가 데이터를 가져와 기록한다.

**중계 결과는 데이터이고 수명이 짧다.** 올바른 UTF-8이어야 하고, 65,536바이트 이하여야 하며, oah 표시나 비밀처럼
보이는 내용이 없어야 하고, 요청 뒤 300초가 지나면 만료된다. 만든 바로 그 작업, 설계, 필요, 목록에 묶인다. 리뷰
run은 격리된 채로 남아 중계를 받지 않는다(`OAH_CAPABILITY_UNSUPPORTED(review-isolated)`). 사용자가 치르는 비용은
이렇다. 발주 직전에 중계하고, 무엇이든 바뀌면 다시 중계한다.

**oah는 도구를 설치하거나 켜거나 연결하지 않는다.** 플러그인이나 서버가 없으면
`OAH_CAPABILITY_DEPENDENCY_UNSATISFIED`나 `OAH_CAPABILITY_UNAVAILABLE`로 거절된다. 사용자가 치르는 비용은 이렇다.
직접 설치하거나 켠 뒤 다시 계획한다.

## 이 단계들에서 Jev

이번 릴리스에서 Jev는 이 문서의 어떤 스킬이나 도구도 고르지 않는다. coverage, 탐색, 받아들임, 능력 경로는 정해진
규칙을 따르고, 능력 계획은 `jev: unused(not-implemented)`를 기록한다. 네이티브 제안은 어떤 판정도 묻지 않고
`jev_status: unused(native)`를 기록한다. [스킬 선택](concepts.md#스킬-선택)에서 설명하는 스킬
재정렬기는 브리프에 넣을 설치된 스킬의 순서를 바꿀 뿐이고, 어떤 스킬 본문을 넘길지는 바꾸지 않는다.

**Jev는 한 확인 항목의 선택 스킬 순서만 바꿀 수 있다.** 일반 run이 같은 설계 확인 항목에 선택 스킬을 둘 이상
넘기고 Jev가 켜져 있으면(`[rerank] enabled`, `[rerank.judgement] enabled`, 키), oah는 그 확인 항목마다 한 번 각
스킬 본문이 그 항목에 구체적인 지침을 주는지 Jev에게 묻고, 그 순서로 스킬을 놓는다. 필수 스킬은 자리를 지키고,
스킬을 더하거나 빼지 않으며, 본문도 그대로다. 결과는 `skills.json`의 묶음 옆 `skill_fit`에 남는다. Jev가 꺼진
모든 상태(`unused(judgement-off)`, `unused(not-enabled)`, `unused(no key)`, `--no-rerank`의
`unused(disabled-by-brief)`)와 확실하지 않은 답처럼 oah가 적용할 수 없는 답은 받아들인 묶음을 그대로 둔다.
TypeSafe는 답한 id를 엄격하게 확인하므로 스킬 하나를 빠뜨린 답은 `unused(error:bad-response)`가 되고,
`unused(error:dropped)`는 일부만 순위를 매겨 돌려주는 판정 제공자의 코드다. 보내는 것은 검토한 각 `SKILL.md`의
검증된 공개 스냅샷에서 읽은 앞 4,000바이트와 정해진 단계·확인 항목 이름뿐이고, 설계 글이나 작업 글은 보내지
않는다. 사용자가 치르는 비용은 이렇다. run마다 그런 확인 항목 하나에 판정 요청이 한 번 가고, 판정을 묻지 않는
`oah skills plan`은 조언 전의 순서를 보여 준다.

## 이번 릴리스의 한계

- 참조 읽기는 run이 끝난 뒤 스트림에서 확인한다. 자식이 파일을 읽기 전에 일을 시작해도 oah는 막지 못하고, run을
  실패시키는 대신 읽지 않은 파일을 기록한다. 자식이 띄운 서브에이전트의 읽기는 세지 않는다.
- `oah fix`의 수정 run은 스킬 본문을 담지만 `skills.json`에 묶음을 기록하지 않고, 시작할 때 설치본 해시를 다시
  재지 않는다. `oah fix`는 중계 입력을 받지 않으므로, 스킬이 리드만 가진 도구를 필요로 하는 수정은 거절된다.
- 경로는 run이 시작하기 전에 정한다. oah는 자식의 도구 시작을 지켜보지 않으며, 그것이 있어야 하는 `direct`
  경로를 보고하지 않는다.
- 예산은 고정된 바이트 수다. 모델의 문맥 창을 재지 않는다.
- 작업 선택은 모든 `oah call` run과 네이티브 제안에 들어가지만, 고정된 입력을 쓰는 `oah fix` 수정에는 들어가지
  않는다. 필요의 요구는 패킷의 의도 절 전체이며, 그 안의 범위를 고르는 방법은 아직 없다.
- 네이티브 서브에이전트의 읽기는 `oah skills read`로만 보인다. 호스트가 직접 읽은 파일은 관찰하지 않는다.
  `oah call settle`은 능력 중계를 다시 확인하지 않는다.
- 작업 패킷의 의도 글을 읽을 수 있는 유일한 Jev 질문인 능력 필요 질문은(그것도 기본으로 꺼진
  `[rerank.judgement] send_task_text = true`일 때만) 아직 어떤 단계도 묻지 않는다. 능력 계획은 정해진 규칙을 따른다.
