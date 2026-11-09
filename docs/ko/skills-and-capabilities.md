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
`OAH_SKILL_MANDATORY_MISSING`, `OAH_SKILL_REFERENCE_UNAVAILABLE`, 또는 이유(`user-only`, `license`, `host`,
`marker`, `pin-mismatch`)가 붙은 `OAH_SKILL_NOT_DELIVERABLE`이다. 다시 시도하지도, 다른 길로 돌아가지도 않고,
run 폴더도 만들지 않는다. 넘길 수 없는 선택 스킬은 건너뛰고 목록에 남긴다. 사용자가 치르는 비용은 이렇다.
검토가 없거나 고정값이 어긋나면 다시 검토하거나 스킬 연결을 끊을 때까지 발주가 막힌다.

**바이트 예산은 고정돼 있다.** 스킬 본문은 196,608바이트까지, 브리프 전체는 524,288바이트까지, 항목은 128개까지다.
예산을 넘으면 호출은 `OAH_SKILLS_BUDGET_EXCEEDED`로 거절되고 스킬마다 크기를 보여 준다. 그것을 보고 작업을
나누거나 선택 스킬의 연결을 끊는다. 사용자가 치르는 비용은 이렇다. 스킬이 많으면 작업을 여러 개로 나눠야 한다.

**사람이 직접 부르는 스킬은 사람에게 남는다.** 사람만 부를 수 있는 스킬은 넘기지 않는다. 그런 스킬이 필수면
`OAH_SKILL_NOT_DELIVERABLE(user-only)`로 거절된다. 사용자가 치르는 비용은 이렇다. 그런 스킬은 직접 실행한다.

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

이번 릴리스에서 Jev는 이 문서의 어떤 것도 고르지 않는다. coverage, 탐색, 전달, 능력 경로는 정해진 규칙을 따르고,
능력 계획은 `jev: unused(not-implemented)`를 기록한다. [스킬 선택](concepts.md#스킬-선택)에서 설명하는 스킬
재정렬기는 브리프에 넣을 설치된 스킬의 순서를 바꿀 뿐이고, 어떤 스킬 본문을 넘길지는 바꾸지 않는다.

## 이번 릴리스의 한계

- 스킬 본문은 언제나 브리프 안에 넣어 넘긴다. 대신 경로를 넘기는 참조 방식은 아직 없다.
- `oah fix`의 수정 run은 스킬 본문을 담지만 `skills.json`에 묶음을 기록하지 않고, 시작할 때 설치본 해시를 다시
  재지 않는다. `oah fix`는 중계 입력을 받지 않으므로, 스킬이 리드만 가진 도구를 필요로 하는 수정은 거절된다.
- 경로는 run이 시작하기 전에 정한다. oah는 자식의 도구 시작을 지켜보지 않으며, 그것이 있어야 하는 `direct`
  경로를 보고하지 않는다.
- 예산은 고정된 바이트 수다. 모델의 문맥 창을 재지 않는다.
