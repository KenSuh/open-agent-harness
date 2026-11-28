## open-agent-harness 규칙

**워커(`OAH_WORKER=1`).** 세션 시작 절차를 건너뛰고 발주 작업만 한다. 구현·수정은 발주자의 리스 워크트리에서 한다. 리뷰는 읽기 전용이며 별도 트리를 쓸 수 있다.

**리드 초안.** 작성 전 `oah recommend --kind design --task <task> --request-file <요청파일>`을 실행하고, 결과의 `oah-<tier>`에 초안을 맡긴다. 패킷은 `--kind packet`을 쓴다.

**안내형 설계(Claude Code 리드, Python CLI).** 설계를 시작하거나 이어 달라는 요청 시 프로젝트 `grilling`을 읽고 `oah design show PROJECT DESIGN`, `oah design coverage PROJECT DESIGN --next`를 실행한다. 미결 질문 하나와 추천 답을 제시하고 기다린다. 저장 요청 시 현재 리비전과 digest로 `oah design save`에 답과 미결을 기록한다. 저장 전 `coverage edit`으로 미리 본다.

- `deferred`: `reason`, `impact`, `resolve_by`, 빈 `answer_refs`.
- 선택 단계: `set-applicability`, `value`는 `not-applicable`, `reason`.

`oah design coverage PROJECT DESIGN --revision N --check` 통과 후 N과 DIGEST를 별도 승인받아 `oah design accept`한다. 구현 요청 시 `oah design check PROJECT DESIGN --revision N --digest DIGEST --accepted` 후 `oah packet new TASK --design PROJECT/DESIGN --design-revision N`을 실행한다. 패킷의 승인 리비전과 digest가 같은지 확인한다. 승인이 바뀌면 멈추고 검사한 어느 판을 구현할지 사용자에게 묻는다.

1. 1차 구현은 Claude Code. Codex CLI 가 정해진 라운드에서 리뷰·수정·재리뷰한다(`oah review`, `oah fix`). 구현자는 리뷰 지적을 리드에게 반환한다.
2. 워커는 발주자의 리스를 쓰며 시작·갱신·종료하지 않는다. 범위 안에서 편집하고 변경과 증거를 돌려준다. 발주자는 편집 전 `oah start --task <task> --agent <you> --scope <path>`(`--scope` 반복 가능), 범위 확대 시 `oah renew --scope …`, 종료 시 `oah finish --summary "…"`를 쓴다. 겹치면 멈추고 보고한다.
3. `.oah/tasks/<task>/PACKET.md`에 의도·범위·계획·수용 조건을 둔다. 승인된 의도를 지킨다. 목표 변경 시 Revision을 올린다. `INTENT`와 조건별 `AC<n>: met|unmet|deferred`를 반환한다.
4. 시작 전 `oah preflight`. ABORT면 중단한다.
5. 수용 조건별 `GATES.md`에 CHECK·EXPECT·실측 EVIDENCE를 기록한다. CHECK를 실행하고 보고한 숫자를 각각 검사한다.
6. 바뀐 만큼만 검증한다. 라운드 사이 결속 게이트(`oah impact --changed-since <rev>`)와 구조 검사를 돌린다. 무거운 게이트는 최종 head에서 한 번 돈다.
7. 실패·생략·이월을 밝힌다. 작성 엔진 리뷰는 독립 리뷰가 아니며 이를 명시한다.
8. 워커는 푸시하지 않는다. 병합·강제 푸시·공유 이력 재작성·브랜치 삭제도 금지. 리드는 `oah finish` 후 푸시한다.
9. 키 금지. 키·토큰·비밀번호·쿠키·계정 ID를 파일·커밋·픽스처·로그에 넣지 않는다. 환경 변수나 OS 키체인에서 읽고 문서·템플릿은 `${ENV_VAR}`를 쓴다.
10. `oah wiki new`·`oah wiki log`로 `docs/wiki/`에 결정·발견과 출처를 남긴다. `oah wiki lint`는 보고만 한다. 소유자만 정할 사항은 멈추고 묻는다.
