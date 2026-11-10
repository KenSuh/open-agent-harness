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
  `OAH_COVERAGE_INCOMPLETE`와 함께 1로 끝난다. `--next`는 다음에 다룰 단계 하나를 알려 준다
  ([이 단계들에서 Jev](#이-단계들에서-jev) 참고).
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
- `oah skills review --links P/D`는 설계의 현재 coverage가 링크한 검토 스킬을 하나에 한 번씩, 최대 16개까지 다시
  확인한다. upstream이 움직이지 않은 링크는 아무것도 출력하지 않는다. 움직인 upstream은 새 커밋에서 검토해 제안으로
  출력하고, 링크는 사용자가 새 검토를 링크하고 설계를 수락할 때까지 원래 pin을 지킨다. 실패한 확인은
  `failed, not checked`로 적히고 명령은 69로 끝난다.
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

**다시 확인은 제안할 뿐 pin을 옮기지 않는다.** 사용자가 치르는 비용은 이렇다. upstream 스킬이 바뀐 뒤 그 새 본문은
사용자가 제안된 검토를 읽고, 링크하고, 설계를 다시 수락한 뒤에야 워커에게 간다.

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

**준비 신원은 하나다.** 로컬 입장은 `[skills] exclude`, 필요의 host·scope, 필수 선택, 현재 능력 승인·schema,
중계 자격·만료, 작업·lease 세대·엔진·단계를 확인한다. 제외한 필수 스킬은 거절하고, 빠진 선택 스킬은 정확한
이유를 남긴다. 검색·검토·갱신·스킬 판정은 비활성·캐시·재시도 경로에서도 살아 있는 입장이 필요하다. 입장 없는
직접 API 호출은 전송 전에 거절한다. `PreparedE`는 이 사실과 검증한 대상 프로필·최종 렌더 digest를 묶는다.
모든 실행·네이티브 정산은 준비 때 기록한 프로젝트 루트·요청 축·최소 단계를 써서 최종 guard 안에서 현재 사실을
다시 비교한다. 프로젝트 하위 폴더에서 실행한 작업 없는 명령도 그 프로젝트의 정책을 쓴다. 처음에 관리 콘텐츠 없이 준비한 run도
첫 작업 스킬 선택이 생기면 다시 준비해야 한다. 실행·제안 전 준비 창은 5분이다. 네이티브 첫 정산과 같은 값의
재시도는 현재 신원을 비교하면서 제안 자체의 24시간 창을 쓴다. 지출 뒤에는 제안 때의 부모 도구 목록·중계 근거를
보존하며 짧은 유효기간의 입력을 다시 읽지 않는다. 수정 작업은 현재 설정·정책으로 스킬을 다시 입장시키고
원래 run의 동결된 부모 목록과 허용된 중계 바이트를 쓴다. 준비·부모 목록·중계의 시간 창은 최초 실행·제안
전에만 검사하며 정산·수정에서는 다시 만료시키지 않는다. 저장한 digest와 현재 설계·패킷·필요·schema·권한·정책은
일치해야 한다. 발주 단계에 따른 coverage 전달은 같은 수락 설계에서 수정 단계 기준으로 정한다. 신원이 바뀌면 거절하고
이전 기록을 보존한다. 수정 작업은 기존
수정 인터페이스의 `OAH_FIX_STATE_CHANGED`로 알린다. 그대로인 네이티브 선택은 한 번 정산하고 같은 재시도에는
같은 영수증을 돌려준다. 수정 작업은 검증한
필요·선택·허용 중계를 동결하고 닫힌 digest/개수/enum 투영만 저장하며, 워커 본문은 메모리에서만 해석한다.
사용자가 치르는 비용: 대기 작업은 5분 뒤, 수정은 관련 로컬 변경 뒤에 다시 준비하고, 네이티브 정산은 제안 자체가
만료되면 새 제안이 필요하다. 수정 때문에 도구 근거를 갱신하는 추가 비용은 없다. 대신 수정은 원래 실행 시점의
도구 결과를 중계 시각 표시와 함께 받는다. 동결 기록이 없거나 바뀌거나 불완전하면 자식을 시작하기 전에 거절한다.
run 기록을 지웠다면 새 run이 필요하다. 직접 API로 입장을 생략할 수 없다.

**요청 경계는 하나다.** `skills find --task`도 발주와 같은 기존 필수 능력·대상 용량·접근 조건을 확인한다.
관리 스킬의 순서 판정도 요청 직전에 그 입장을 다시 확인한다. 작업 없는 탐색과 설계 열람은 각자의 제한된
리드 입장을 유지한다. 관리 검색·수집 HTTP 요청·Git 참조 조회(`ls-remote`)·Git fetch·조언 요청은 I/O 직전에
요청 수가 `null`(미확인)인 시도 기록을 저장한 다음 입장을 다시 확인하고, 각 작업이 반환하거나 실패할 때까지 제한된 시도를
열린 상태로 유지한다. 실제 전송이 시작될 때 메모리의 행이 시작 상태가 된다. 이후 요약 저장이 실패하면 앞선
요청 수는 미확인으로 남고 결과 저장 실패를 경고하며, 전송 결과나 거절은 그대로 전달한다. 최종 guard에서 거절하면
요청 수 0을, 전송이 반환하면 1을 저장한다. 끝 저장 전에 프로세스가 중단되면 요청 수는 미확인으로 남는다.
최종 guard와 전송 사이에는 파일을 쓰지 않는다. Git URL 변환은
로컬 처리이며 요청 시도를 만들지 않는다. 사용자가 치르는 비용:
필수 능력이 없거나 로컬 입력이 바뀌면 명시적으로 고치고 새로 준비해야 다음 요청을 보낼 수 있으며, 요청·토큰을
쓴 실패 작업은 재시도 전에 그 사실을 확인할 수 있다.

**일반 작업은 기존 경로를 따른다.** 관리 전달·탐색·능력 입력·허용되고 설정된 작업 조언이 없으면 같은 프롬프트,
판정과 예약 순서로 실행한다. 전역 rerank·judgement 활성화 플래그만으로는 관리 대상 프로필을 요구하지 않는다.
사용자가 치르는 비용은 없다. 관리 작업에 도달할 수 없는 run은 이전과 똑같이 동작한다.

처음부터 넘길 수 없는 필수 스킬이 있으면 호출은 정해진 코드로 거절된다.
`OAH_SKILL_MANDATORY_MISSING`, `OAH_SKILL_REFERENCE_UNAVAILABLE`(`inaccessible`, `unsupported-engine`, `profile`,
`access`), 또는 이유(`excluded`, `scope`, `user-only`, `license`, `host`,
`marker`, `pin-mismatch`)가 붙은 `OAH_SKILL_NOT_DELIVERABLE`이다. 다시 시도하지도, 다른 길로 돌아가지도 않고,
run 폴더도 만들지 않는다. 넘길 수 없는 선택 스킬은 건너뛰고 목록에 남긴다. 사용자가 치르는 비용은 이렇다.
검토가 없거나 고정값이 어긋나면 다시 검토하거나 스킬 연결을 끊을 때까지 발주가 막힌다.

**검증한 대상 경로는 하나다.** 필수 관리 전달은 정확한 host·CLI·모델·effort·경로에 맞는 현재의
`TargetBudgetProfileV1`이 필요하다. 설정한 토큰 수나 모델 카탈로그만으로는 검증되지 않는다. 패키지의 검증
레지스트리에는 현재 **실제 검증 프로필이 없으므로**, 관리 경로는 판정 전에
`OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)`으로 거절한다. 시험 fixture는 실제 host 검증이 아니다.
프로필은 근거·만료, 유한한 입력 용량, 출력·host·기존 문맥 예약량, 렌더·도구 schema 오버헤드를 고정해야 한다.
판정 전에 전체 도달 가능한 브리프, 설계 ReadPlan의 지연 payload, 필수 참조, 계획한 모든 read/tool envelope를
남은 입력 용량과 비교하고, 정확한 최종 렌더를 로컬에서 다시 확인한다. 사용자가 치르는 비용: 필수 관리 자료는
검증한 경로에서만 동작하며, 그 밖에서는 관리 전달 밖의 inline·더 작은 자료를 쓰거나 작업을 나눠야 한다.
용량 미확인은 어떤 요청보다 먼저 드러난다.

**바이트 한도는 별도로 지킨다.** 직렬화한 inline 스킬 본문은 196,608바이트, 전체 도달 가능한 브리프는 참조
payload 전체 렌더·read envelope를 포함해 524,288바이트까지다. 필수 줄·바이트 슬라이스마다 read envelope 하나를
전체 바이트와 토큰 상한에 각각 한 번 센다. 짧은 줄 29,000개를 최대 2,000줄씩 읽으면 15개가 필요하다.
지연 설계 자료는 별도로 262,144바이트 한도를
지킨다. 스킬 본문·보조 파일을 각각 하나로 세어 payload는 128개까지다. 보조 파일이 26개를 넘더라도 inline으로
기록한 파일은 숫자 라벨로 모두 정확히 한 번 들어간다. 넘으면 `OAH_SKILLS_BUDGET_EXCEEDED`와 측정 합계를
보여 준다. 참조도 전체 렌더·대상 용량 한도를 생략하지 않는다. 사용자가 치르는 비용: 실제로 큰 pack은 나누거나
선택 자료를 빼야 하며, 유효한 27파일 pack은 모든 파일을 받는다. 뒤의 로컬 변경은 앞선 요청 뒤에도 거절될 수 있다.

**큰 파일은 참조로 넘긴다.** 스킬 본문이 스킬 예산에 들어가지 않으면, oah는 스킬의 보조 파일을 큰 것부터 브리프
밖으로 빼고 그 자리에 "Read `<경로>` in full with your Read tool before you start"라는 줄과 줄 수, digest를
적는다. 스킬 본문 자체는 언제나 브리프 안에 남는다. 참조 입장은 모든 플랫폼에서 실제 실행 파일·CLI 버전·도구와
read/access 정책이 공통 task-context 검증을 통과해야 하며 실행 때 신원을 다시 확인한다.
이 방식은 Claude 프로세스 run(`oah call claude`를 앞에서,
백그라운드에서, 또는 Orca 워커로 실행)에서만 쓰고, 작업 디렉터리 안에 고정된 바이트 그대로인 스킬 설치본이
있어야 한다. 예를 들면 `.claude/skills/<이름>/` 아래다. 그 run에서만 oah는 Claude에
`--output-format stream-json --verbose`를 요청하고(다른 인자는 바꾸지 않는다), 스트림의 Read마다 파일 바이트와
맞춰 보고, run 폴더에 `skills-receiving.json`을 쓴다. 필수 파일을 모두 끝까지 읽었으면 `reference_delivery: complete`, 아니면
`incomplete`다. 읽지 않은 파일이 있어도 run의 결과는 그대로 두고 경고를 남기며, 기록은 전달이 끝나지 않았다고
적는다. result 이벤트가 없는 스트림은 `OAH_ENGINE_FAILED`로, 읽기를 기록하지 못하면 `OAH_SKILL_RECEIVING_FAILED`로
run을 실패 처리한다. Codex, 네이티브 서브에이전트, `oah fix` 수정 run은 oah에 읽기를 보여 주지 않고, 네이티브
Windows에는 아직 검증된 읽기 프로필이 없으므로, 그곳에서는 브리프에 들어가지 않는 필수 파일이
`OAH_SKILL_REFERENCE_UNAVAILABLE(unsupported-engine)`으로 거절된다. 작업
디렉터리 밖의 설치본은 `OAH_SKILL_REFERENCE_UNAVAILABLE(inaccessible)`로 거절된다. 엔진을 시작하기 직전에 oah는
참조한 파일의 해시를 다시 잰다. 사용자가 치르는 비용은 이렇다. 그런 스킬은 저장소 안에 설치해야 하고 네이티브 Windows가
아닌 Claude 프로세스 run에서만 돌며, 모델은 파일마다 끝까지 읽느라 컨텍스트를 쓴다.

**스트림은 도는 동안 확인한다.** 같은 run에서 oah는 Claude의 스트림을 오는 대로 읽는다. 첫 줄은 이 run의
Claude Code init 이벤트여야 한다. 세션 ID가 있고, 작업 디렉터리와 oah가 준비한 Claude Code 버전, 권한 모드가 run과
같아야 한다. 그 줄이 없거나 맞지 않거나 JSON이 아니거나 256 KiB보다 크거나 10초(run 제한 시간이 더 짧으면 그
시간) 안에 오지 않으면, oah는 run의 프로세스 트리 전체를 곧바로 멈춘다. 그 run은 `OAH_ENGINE_FAILED`로 실패하고
세지 않는다. init 뒤에도 JSON이 아닌 줄, 다른 세션을 적은 두 번째 init, 다른 세션의 이벤트는 run을 멈추고, 256 KiB보다
큰 줄은 읽지 않고 건너뛴다. oah는 MCP 도구 사용(`mcp__<서버>__<도구>`)마다 그 결과를 짝지어 run 폴더에
`capabilities-receiving.json`을 쓴다. 사용마다 능력 ID, `planned` 또는 `unplanned`, 결과 `ok`, `error`, `denied`,
`unobserved`, 그리고 세션과 사용, 인자, 결과의 digest를 적고, 그 글 자체는 적지 않는다. 이벤트는 16 MiB까지 읽고,
결과를 기다리는 사용은 64개까지 두며, 사용 기록은 1,024개까지 남긴다. 기록을 쓰지 못하면
`OAH_CAPABILITY_RECEIVING_FAILED`로 run을 실패 처리한다. 사용자가 치르는 비용은 이렇다. 느린 MCP 서버가 붙는 동안처럼
시작에 10초보다 오래 걸리는 Claude 세션은 멈추고, 멈춰도 자식이 첫 줄 전에 한 일은 되돌리지 못한다.

**사람이 직접 부르는 스킬은 사람에게 남는다.** 사람만 부를 수 있는 스킬은 넘기지 않는다. 그런 스킬이 필수면
`OAH_SKILL_NOT_DELIVERABLE(user-only)`로 거절된다. 사용자가 치르는 비용은 이렇다. 그런 스킬은 직접 실행한다.

**설치된 사본은 짐작하지 않고 밝힌다.** oah는 설치된 스킬을 프로젝트, 자체 키트 캐시, 사용자 폴더, 설치한 Claude
플러그인 순서로 읽고, 같은 이름은 처음 찾은 사본을 쓴다. 브리프에 고른 스킬이 다른 곳에 내용이 다른 사본으로도
있으면, 브리프의 메모가 각 사본의 출처와 digest를 적는다. Codex의 플러그인 캐시(`CODEX_HOME`, 없으면 `~/.codex`
아래의 `plugins/cache`)는 `[skills] codex_plugin_cache = true`일 때만 읽는다. 그 사본은 맨 뒤로 밀리고
`plugin-cache (not proof of enabled)`로 표시된다. 캐시에는 꺼 둔 플러그인과 예전 버전도 남기 때문이다. 사용자가
치르는 비용은 이렇다. Codex 플러그인 캐시에만 있는 스킬은 설정을 켜기 전에는 쓰이지 않고, 켠 뒤에도 oah는 Codex가
그 플러그인을 켜 두었는지 알려 주지 못한다.

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
그날 첫 호출은 검색을 기다릴 수 있다. `oah fix` 수정은 검색하지 않으며 검증해 동결한 선택을 다시 받아들이고
닫힌 투영만 저장한다.

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
  스킬 본문 뒤의 `## Capabilities` 블록에 결과를 넣어 넘긴다. 호출이 `--skill-needs FILE`을 받으면 `plan`에도
  같은 파일을 준다. 그래야 작업 필요에 고른 스킬도 중계 요청을 받는다.
- 필요마다 저장한 결과를 하나씩 넘긴다. `--relay-result` 플래그 순서는 계획도 거절도 바꾸지 않으며, 한 필요에
  다른 결과 둘이 오면 `OAH_CAPABILITY_RELAY_REQUIRED(conflicting)`로 거절된다.
- oah가 도는 동안 스트림을 확인하는 run에서는(위의 **스트림은 도는 동안 확인한다** 참고)
  `capabilities-receiving.json`이 MCP 도구 사용을 하나씩 적는다. 계획이 고른 도구는 `planned`, 그 밖의 도구는
  `unplanned`이고, oah는 계획에 없는 도구를 기록할 뿐 막지 않는다.
- `.oah/config.toml`의 `[capabilities]`에는 서버에 oah의 능력 분류 하나를 붙이는 `classes = {server = "class"}`,
  능력 사이 의존을 더하는 `requires = {"<id>" = ["<id>", ...]}`, 정확한 능력 하나를 Jev가 읽을 수 있는 공개 문서
  파일에 잇는 `docs = {"<id>" = "SOURCE:PATH"}`가 있다([이 단계들에서 Jev](#이-단계들에서-jev) 참고). 플러그인의
  서버는 그 플러그인이 켜져 있어야 한다. 의존은 최대 세 번에 걸쳐 풀고, 순환이 있으면 찾은 고리를 적어 거절한다.

**설정에 적힌 이름은 연결이 아니다.** 필수 필요는 새로 받은 중계로만 채운다. 설정에 이름만 있는 도구는
`OAH_CAPABILITY_UNSUPPORTED(unknown-schema)`나 `(pending-approval)`로 거절된다. 사용자가 치르는 비용은 이렇다.
필수 도구라면 발주 전에 리드가 데이터를 가져와 기록한다.

**중계 결과는 데이터이고 수명이 짧다.** 올바른 UTF-8이어야 하고, 65,536바이트 이하여야 하며, oah 표시나 비밀처럼
보이는 내용이 없어야 하고, 요청 뒤 300초가 지나면 만료된다. 만든 바로 그 작업, 설계, 필요, 목록에 묶인다. 리뷰
run은 격리된 채로 남아 중계를 받지 않는다(`OAH_CAPABILITY_UNSUPPORTED(review-isolated)`). 사용자가 치르는 비용은
이렇다. 발주 직전에 중계하고, 무엇이든 바뀌면 다시 중계한다.

**능력 필요는 무엇이든 묻기 전에 확인한다.** 아무도 줄 수 없는 필수 능력, 채워지지 않은 의존, 그리고 없거나
만료됐거나 실패했거나 낡았거나 서로 부딪히는 중계는 스킬 입장과 함께, 스킬 검색·Jev 질문·예산 확인·run 폴더보다
먼저 거절된다. 보통 호출과 네이티브 제공이 같다. 검색과 Jev 조언 뒤에는 선택적 분류 필요까지 넣어 계획을 다시
만들고 한 번 더 확인한다. 사용자가 치르는 비용은 이렇다. 거절된 호출은 그 시도의 스킬 탐색을 보여 주지 않고, 한
도구를 두 번 중계했다면 넘길 결과를 직접 고른다. oah는 하나를 대신 고르지 않는다. 두 확인 사이에 만료된 중계는
이미 보낸 검색과 질문 뒤에 거절된다.

**oah는 도구를 설치하거나 켜거나 연결하지 않는다.** 플러그인이나 서버가 없으면
`OAH_CAPABILITY_DEPENDENCY_UNSATISFIED`나 `OAH_CAPABILITY_UNAVAILABLE`로 거절된다. 사용자가 치르는 비용은 이렇다.
직접 설치하거나 켠 뒤 다시 계획한다.

## 이 단계들에서 Jev

이 문서에서 Jev는 아무것도 혼자 정하지 않는다. coverage, 탐색, 받아들임, 능력 경로는 정해진 규칙을 따른다. Jev는
그 규칙이 이미 허락한 선택지 사이에서만 고를 수 있고, oah는 판정이 같은 입력으로 다시 확인된 뒤에만 답을 적용한다.
Jev가 꺼져 있거나, 답이 확실하지 않거나 낡았거나, 요청이 실패하면 정해진 순서를 그대로 쓴다. Jev는
`[rerank] enabled`, `[rerank.judgement] enabled`, 키가 모두 있을 때 켜진다. 사용자가 쓴 글은 각자의 허락이 있을
때만 나간다. 설계 글은 `send_design_text`, 작업 패킷의 의도 글은 `send_task_text`이며 둘 다 기본으로 꺼져 있다.
네이티브 제안은 어떤 판정도 묻지 않고 `jev_status: unused(native)`를 기록한다. [스킬 선택](concepts.md#스킬-선택)에서
설명하는 스킬 재정렬기는 브리프에 넣을 설치된 스킬의 순서를 바꿀 뿐이고, 어떤 스킬 본문을 넘길지는 바꾸지 않는다.

**Jev는 한 확인 항목의 선택 스킬 순서만 바꿀 수 있다.** 일반 run이 같은 설계 확인 항목에 선택 스킬을 둘 이상
넘기고 Jev가 켜져 있으면, oah는 그 확인 항목마다 한 번 각
스킬 본문이 그 항목에 구체적인 지침을 주는지 Jev에게 묻고, 그 순서로 스킬을 놓는다. 필수 스킬은 자리를 지키고,
스킬을 더하거나 빼지 않으며, 본문도 그대로다. 결과는 `skills.json`의 묶음 옆 `skill_fit`에 남는다. Jev가 꺼진
모든 상태(`unused(judgement-off)`, `unused(not-enabled)`, `unused(no key)`, `--no-rerank`의
`unused(disabled-by-brief)`)와 확실하지 않은 답처럼 oah가 적용할 수 없는 답은 받아들인 묶음을 그대로 둔다.
TypeSafe는 답한 id를 엄격하게 확인하므로 스킬 하나를 빠뜨린 답은 `unused(error:bad-response)`가 되고,
`unused(error:dropped)`는 일부만 순위를 매겨 돌려주는 판정 제공자의 코드다. 보내는 것은 검토한 각 `SKILL.md`의
검증된 공개 스냅샷에서 읽은 앞 4,000바이트와 정해진 단계·확인 항목 이름뿐이고, 설계 글이나 작업 글은 보내지
않는다. 사용자가 치르는 비용은 이렇다. run마다 그런 확인 항목 하나에 판정 요청이 한 번 가고, 판정을 묻지 않는
`oah skills plan`은 조언 전의 순서를 보여 준다.

**Jev는 어느 검색을 먼저 돌릴지 고를 수 있다.** `oah skills find`의 필요에
`--tech auth=passkey --tech platform=supabase`처럼 공개 기술 단어가 둘 이상 있으면, oah는 어느 단어의 질의가 그
필요를 덮는지 Jev에게 묻고 그 질의를 먼저 돌려 요청 예산이 거기에 닿게 한다. 나가는 것은 제공자 이름과 공개
단어뿐이고 정해진 라벨로 나가며, 질의는 oah 자신의 틀 그대로다. 보고서의 `search_path`에 선택지, 먼저 돌린 것,
`jev`가 남는다. 공개 단어가 하나이거나, Jev가 꺼져 있거나, oah가 적용할 수 없는 답이면 틀의 순서를 지킨다.
사용자가 치르는 비용은 이렇다. `find`마다 그런 필요 하나에 판정 요청이 한 번 간다. `oah call` 안의 탐색은 틀의
순서를 그대로 쓴다.

**Jev는 다음 설계 단계를 제안할 수 있다.** `oah design coverage P D --next`는 현재 판에서 처음으로 열린 단계를
알려 준다. 열린 단계가 둘 이상이고 `send_design_text = true`이면, oah는 그중 어느 단계가 풀리지 않은
결정을 덮는지 Jev에게 묻고 그 단계를 대신 알려 준다. 이때 단계 이름과 설계 글의 앞부분(최대 4,096바이트)을
보낸다. 출력은 `next: <stage> (jev=..., applied=yes|no)` 꼴이다. 열린 단계가 하나면 아무것도 묻지 않고, 하나도
없으면 출력이 `complete`나 `blocked`라고 말한다. 사용자가 치르는 비용은 이렇다. `--next`마다 판정 요청이 한 번
간다. 제안일 뿐이라 아무것도 저장하지 않고 coverage도 그대로다.

**Jev는 작업의 부분마다 능력을 고를 수 있다.** 일반 `oah call`(네이티브 제안도 리뷰도 아닌 run)에서 설계가 없는
작업 필요는 각각 패킷 의도 절의 한 구간을 덮는다. `send_task_text = true`이면 oah는 그 구간만 보내 oah의 능력
분류 중 무엇이 필요한지 Jev에게 묻는다(`capability-needs/v1`). 예로 읽힌 분류는 선택 필요가 된다. 그 후보는 그
분류의 자격 있는 항목이며, 이번 릴리스에서는 리드의 목록(`--parent-inventory`)이 현재 증거와 읽기 전용 효과로
보고한 항목을 목록 순서대로 쓴다. 앞의 여덟 개가 선택지가 되고 나머지는 미뤄진다. 모든 선택지에
`[capabilities] docs`로 이은 문서가 있으면 oah는 어느 선택지의 문서에 적힌 기능이 그 구간에 맞는지 Jev에게
묻고(`capability-select/v1`) 그 항목을 올린다. 그렇지 않으면 첫 항목을 쓴다. 고른 항목은 `## Capabilities` 블록에
선택 경로로 나오고, oah는 그것을 호출하지 않는다. `capabilities.json`에는 분류 판정마다 `class_advice_refs`, 선택마다
`entry_advice`가 결과와 함께 남는다. 결과는 `selected`, `no-match`, `uncertain`, `off`, `error`, `stale`,
`single-option`, `no-candidate`, `no-public-candidate-evidence` 중 하나다. 분류 필요는 run을 거절하지 않고, 중계를
요청하지 않으며, 준비 id를 바꾸지 않으므로 저장한 중계 결과는 그대로 유효하다. `oah capabilities plan`은 아무것도
묻지 않고 `jev: unused(plan-pure)`를 기록한다. 사용자가 치르는 비용은 이렇다. run마다 작업 필요 하나에 판정
요청이 한 번, 문서가 이어진 선택지가 둘 이상인 분류 하나에 한 번 더 가고, 요청마다 패킷 글의 그 구간이 실린다.

**Jev는 사용자가 이은 문서로만 항목을 비교한다.** `docs = {"mcp:docs/lookup" = "docs:document-text"}`는 정확한
능력 ID 하나에 `oah sources`로 등록한 출처와 그 검증된 스냅샷의 파일을 잇는다(문서 페이지는 `document-text`,
저장소는 `README.md` 같은 경로). oah는 그 파일의 앞 4,000바이트를 스냅샷에서 다시 읽어 보낸다. 항목을 이름으로
맞추지 않으며, 설정에 이름만 있는 서버는 선택지가 되지 않는다. 사용자가 치르는 비용은 이렇다. 출처를 등록하고
Jev가 비교할 항목마다 문서를 이어야 하며, 하나라도 빠지면 그 분류는 첫 항목을 쓴다.

## 이번 릴리스의 한계

- 참조 읽기는 run이 끝난 뒤 스트림 전체에서 짝짓는다. 자식이 파일을 읽기 전에 일을 시작해도 멈추지 않고, run을
  실패시키는 대신 읽지 않은 파일을 기록한다. 자식이 띄운 서브에이전트의 읽기는 세지 않는다.
- 도는 동안의 확인은 관찰이지 장벽이 아니다. 자식은 첫 줄 전에도 일할 수 있고, oah는 그 뒤에야 멈춘다. 이 확인은
  oah가 스킬 참조 때문에 직접 stream-json을 켠 run에서만 돈다. 설계 전송이 있는 run, Codex run, 네이티브
  서브에이전트, `oah fix` 수정은 도는 동안 읽지 않는다.
- `oah fix`의 수정 run은 검증한 스킬 본문을 메모리로 전달하고 `skills.json`에는 닫힌 E 투영을 저장한다.
  시작할 때 현재 사본과 동결한 허용 중계를 다시 확인한다. 명령은 새 중계 입력을 받지 않으므로 필수 중계는
  검증한 일반 준비에 이미 속해 있어야 하고 아직 유효해야 한다.
- 경로는 run이 시작하기 전에 정한다. init 이벤트는 도구 이름만 알려 주므로 어떤 도구 사용도 스키마와 맞춰 보지
  않고(`schema_digest`는 null), oah가 승인할 때까지 일을 시작하지 않고 기다리는 호스트가 있어야 하는 `direct`
  경로는 보고하지 않는다.
- 실제 대상 검증은 아직 남아 있다. 근거·만료를 갖춘 측정 프로필의 검토 전에는 빈 레지스트리가 관리 전달을
  거절한다. 카탈로그 문맥 크기는 그 검증을 대신하지 못한다.
- 작업 선택은 모든 `oah call` run과 네이티브 제안에 들어가며, `oah fix`는 검증한 일반 선택을 고정된 입력으로
  전달한다. oah가 이끌어 내거나 `oah skills find --task`가 쓰는 필요는 패킷의 의도 절 전체를 덮는다. 리드는 필요
  파일에서 그것을 바이트 범위로 좁힐 수 있고, oah는 그 범위를 패킷과 다시 맞춰 본다. 명령에는 범위를 고르는
  방법이 없다.
- 네이티브 서브에이전트의 읽기는 `oah skills read`로만 보인다. 호스트가 직접 읽은 파일은 관찰하지 않는다.
  `oah call settle`은 첫 정산과 영수증 재사용 전에 기록된 능력 중계 신원을 비교한다. 정산에는 제안의 24시간
  창을 적용하며, 새 지출에는 갱신한 중계 근거가 필요하다.
- 능력 분류는 Jev와 스킬이 이름 붙인 도구에서만 온다. 발주의 단계와 종류는 분류를 더하지 않고, 능력 ID를 적은
  패킷 줄은 읽지 않으며, 분류로 고른 항목은 자기 의존을 끌어오지 않는다.
- 후보가 없는 분류 필요는 다른 계획할 것이 없으면 계획을 남기지 않으므로, 그 run에는 `capabilities.json`이 쓰이지
  않는다.
- Jev는 `oah skills find`에서만 검색 순서를 고른다. `oah design coverage --next`는 단계를 제안할 뿐이고, 이번
  릴리스에서 설계 시작과 재개는 그것을 묻지 않는다.
