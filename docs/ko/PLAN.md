[English](../PLAN.md)

# open-agent-harness — Phase 0 계획

상태는 **2026-10-23 소유자 승인**이다. 소유자 결정은 13절, 개정 1–5(2026-10-23)는 15절, 의도 개정 3은 1절에 있다.

이 계획은 한 소유자가 혼자 쓰던 비공개 다중 에이전트 하네스(아래에서 "원본"이라 한다)를
한 사람이 macOS나 Windows에 설치해 자기 저장소에서 쓸 수 있는 범용 키트로 바꾼다.
4절은 원본의 모든 구성 요소와 처리 방식을 나열한다. 5–11절은 무엇을 만들지 설명한다.
13절은 소유자가 결정한 것과 구현자가 결정한 것을 구분한다.

---

## 1. 의도

<!-- OAH_INTENT:BEGIN -->
## Intent
Intent-ID: open-agent-harness
Revision: 4
Status: accepted
### Problem
AI로 실무 수준의 작업을 하는 개발자에게는 Claude Code와 Codex CLI라는 강력한 도구 두 개가 있다. 하지만 둘을 함께 믿고 쓰게 해 주는 작업 환경은 주어지지 않는다.
- 공유 리스
- 단계별 라우팅
- 증거 게이트
- 한계가 정해져 있고 정직하게 표시되는 리뷰
- 프로젝트 기억
- 영향 지도
- 최신 상태를 유지하는 스킬과 모델

소유자는 이 환경을 한 컴퓨터에 직접 만들었다. 다른 사람은 이를 스스로 다시 만들어야 한다.
### Desired outcome
macOS나 Windows에서 설치 명령 하나와 `oah init`으로 새 프로젝트나 기존 프로젝트에 이 환경을 자동으로 만들고, 소유자의 하네스처럼 실행한다.
- Claude Code가 1차 구현을 한다.
- Codex가 리뷰, 수정, 재리뷰를 한다.
- 위키, 영향 지도, 게이트, 훅을 만들고 최신 상태로 유지한다.
- 모델과 스킬은 업스트림 업데이트를 따라간다.

그 결과 기업 수준의 리뷰 절차(검증 가능한 리뷰, 정확한 head에 결속한 증거, 점검한 반영)를 서버 없이 로컬에서 지킨다.

유료이거나 되돌릴 수 없는 일은 사용자가 명시적으로 확인해야만 일어난다.
### Success conditions
- AC1: 새 macOS 13+ 또는 Windows 11 컴퓨터에서 README의 명령 하나로 `oah`를 설치하고, 새 터미널에서 `oah --version`이 버전을 출력한다.
- AC2: 새 프로젝트나 기존 프로젝트에서 `oah init`이 빠진 도구를 공식 설치 명령과 함께 모두 나열하고 사용자가 확인한 도구만 설치한다. 각 도구 자체 로그인 명령으로 사용자가 Claude Code와 Codex에 로그인하게 하며, `oah`는 비밀번호, 키, 토큰을 보지 않는다.
- AC3: `oah init` 뒤 프로젝트에 규칙 블록, tier 에이전트, Obsidian 호환 위키, 최신 영향 지도, 게이트 템플릿, 사용자가 수락한 훅이 있다.
  - 기본값은 소유자의 작업 흐름과 같다. Claude가 1차 구현을 하고 Codex가 리뷰, 수정, 재리뷰를 한다.
  - 작업을 계속하는 동안 영향 지도와 위키는 자동으로 최신 상태를 유지한다. 커밋, 병합, 작업 단계마다 갱신하고 로그, 색인, 정리 상태를 관리한다.
- AC4: 자기 프로젝트의 첫 작업(또는 `examples/hello-oah`)에서 사용자가 패킷, 사전 점검, 리스, Claude 구현, 게이트, Codex 리뷰와 수정, 재리뷰, 완료까지 전체 반복 과정이 끝나는 것을 본다. 각 단계의 결과를 쉬운 한국어나 영어로 설명한다.
- AC5: 새 모델이나 검증된 스킬 업데이트가 있으면 oah의 업데이트 확인 뒤 다음 작업에서 수동 절차 없이 사용하며, 무엇이 언제 왜 바뀌었는지 보여 준다. 모델은 매 실행 전에 확인하고, 스킬은 기본적으로 적어도 하루 한 번 확인한다. 사용자가 고정한 모델이나 스킬은 알리지 않고 바꾸지 않는다.
- AC6: 결정을 묻는 모든 메시지는 무엇이 일어나고 비용이 얼마인지 밝힌다. 구매, 계정 변경, push, 병합, 전역 파일 편집처럼 유료이거나 되돌릴 수 없는 일은 사용자가 명시적으로 확인해야만 일어난다.
- AC7: 저장소에는 소유자 하네스의 비공개 데이터나 어떤 종류의 자격 증명(API 키, 토큰, 비밀번호)도 없으며 A17의 스캔으로 이를 강제한다. 함께 배포하는 모든 외부 항목에 출처, 버전, 라이선스를 표시한다.
### Constraints
- macOS 13+와 Windows 11에서 네이티브로 실행한다(Windows 10 22H2는 최선 노력 지원). Python 3.11+와 표준 라이브러리만 쓴다(3절).
- 소유자의 비공개 하네스에 런타임으로 의존하지 않는다. `oah`는 새로 작성하는 이식으로 이를 따라간다(A16).
- 외부 구성 요소는 라이선스가 허락하는 범위에서만 연결하거나 의존하거나 저장소에 그대로 번들한다(A13).
### Non-goals
- 팀 서버, 웹 UI, 원격 사용 기록 전송. 작은 팀은 개발자별·저장소별로 `oah`를 쓴다.
- 기업 관리 기능. 싱글 사인온, 중앙 감사 로그, 조직 정책 관리는 제공하지 않는다.
- 프로그래밍이나 git 교육. `oah`는 사용자가 git, 브랜치, pull request를 안다고 전제한다.
- Linux를 지원 OS로 삼는 것. 동작할 수는 있지만 테스트하지 않는다.
### Open questions
- 없다.
<!-- OAH_INTENT:END -->

**개정 이력.** 개정 1은 소유자가 이 계획과 함께 승인한 1절의 내용이다(PR #1). 개정 2
(2026-10-23)는 소유자의 제품 구상을 따르며, 원문은 다음과 같다. «우리 하네스를 그대로 초보 사용자가 본인 처음 AI를
통해 프로그래밍 할 때 자동으로 본인 하네스 환경이 우리 환경 처럼 구축되고 진행되도록 하는 깃 오픈 소스를 만들고 싶은거야».
AC3과 AC7에는 승인할 때 권고한 두 가지 추가 사항을 담았다. 개정 3(2026-10-23)은
리뷰 뒤 소유자의 결정에 따라 AC5에 업데이트 확인이라는 조건을 붙였다. 개정 4(2026-10-24)는
소유자의 다음 결정에 따라 대상을 바꿨다. «설명을 엔터프라이즈급 개발 진행하는 분들에게 도움이되는 부분으로 가볍게쓸 초보자하고 우리 하네스하고는
안맞지 않아?». 권고한 표현을 받아들여, 실무 수준의 작업을 하는 개발자와 작은 팀이
기업 수준의 리뷰 절차를 로컬에서 지킨다고 설명한다. `oah`는 기업 관리 제품도,
첫 프로그래밍 도구도 아니다. 뒤에서 여전히 "초보자"라고 하는 절(6.5, 개정 이력,
OB)은 이력을 보존한다. P9 레인에서 이 대상을 위한 사용자 문서와 README를 다시 쓴다.

**GitHub는 선택 사항이다.** 따라 하기 예제는 새 `git init` 저장소에서 완전히 로컬로 실행된다. push와
PR은 선택하는 마지막 단계다. `gh`와 원격 저장소가 있으면 `oah`가 `gh pr create` 명령을 출력하고,
`oah review summary`가 PR 본문을 출력한다.

**릴리스 경로.** v1은 이 비공개 저장소에서 만든다. 공개 여부는 최종 스캔 뒤 소유자가 결정한다.
설치 문서는 클론 방식(지금 동작함)과 git 없이 고정 릴리스로 설치하는 부트스트랩
(공개 뒤, 6.5)을 다룬다. PyPI 배포는 나중에 소유자가 결정한다.

**대상.** Claude Code와 Codex로 실무 수준의 작업을 하는 개발자와 작은 팀이다.
오래 유지하는 코드베이스, 잘못되면 비용이 큰 변경(데이터, 권한, 과금), 한 저장소에서 여러
에이전트가 함께 하는 작업을 다룬다. 이들은 코드가 반영되기 전에 검증 가능한 리뷰와 증거를 원한다.
개발자별·저장소별로 실행하고 Claude Code와 Codex 가운데 적어도 하나가 필요하다(둘 다 있으면 가장 좋다).
git과 pull request에 익숙하다고 전제한다. 설치 프로그램과 안내형 `oah init`은 시작을 쉽게 하는 편의 기능이며,
프로그래밍을 가르치겠다는 약속이 아니다.

## 2. 목표, 비목표, 피해야 할 결과

**목표(모두 작업 지시에서 가져왔다).**
- 병렬 세션과 워크트리가 충돌하지 않게 하는 공유 잠금/리스 CLI.
- 단계(1차 구현 → Claude, 리뷰와 수정 → Codex)와
  난이도(tier → 모델과 추론 강도)에 따른 엔진 라우팅.
- unlazy를 바탕으로 한 게이트 원장(CHECK / EXPECT / EVIDENCE)과 Stop 훅.
- 원래 의도와 승인 조건을 담는 작업 배정 패킷.
- 작업 전 사전 점검.
- 프로젝트별 지식 위키, 그래프(graphify), 코드 영향 지도.
- 일반화할 수 있는 리뷰어 신뢰 원칙. 라운드 추적, 카나리, 정제한 스냅샷, 정직한 독립성 표시.
- 이 환경을 자동으로 만드는 안내형 온보딩(6.5).
- 자동으로 선택하고 업데이트해 사용하는 스킬과 모델(A9, A10, A14, A15).

**v1의 비목표.**
- Linux를 지원 OS로 삼는 것. 아마 동작하겠지만 테스트하지 않는다.
- WSL을 필수로 요구하는 것.
- 팀 또는 서버 모드, 웹 UI, 원격 사용 기록 전송. 사용 기록은 로컬에 둔다(A9).
- Orca를 기본으로 쓰는 것. 선택형 Orca 어댑터를 계획한다(8행).
- 샌드박스, 로그인, 서브에이전트 등 Claude Code나 Codex가 이미 하는 일을 다시 구현하는 것.

**피해야 할 결과("동작"해도 실패다).**
- 새 사용자가 처음으로 유용한 실행을 하기 전에 긴 설명서를 읽어야 한다.
- 키트가 묻지 않고 셸 프로필, 전역 에이전트 설정, 훅 같은 전역 항목을
  설치하거나 바꾸거나 삭제한다.
- 원본의 비공개 데이터가 이 저장소로 샌다.
- 라이선스가 허락하지 않는데 외부 코드를 번들하거나, LICENSE, 고정한 업스트림
  커밋, 고지 없이 번들한다(A13).
- 어떤 종류든 자격 증명이 저장소, 이력, 릴리스, 이슈, CI 로그에 들어간다(A17).
- bash에서는 동작하지만 PowerShell에서는 깨지거나, 작성자의 Mac에서만 동작한다.
- 신뢰를 꾸며 낸다. 표시나 영수증이 그 장치가 증명하는 것보다 더 많은 것을 주장한다.

## 3. 제약

- **플랫폼.** macOS 13+와 Windows 11이다(Windows 10 22H2는 최선 노력 지원이다. Codex 자체가
  Windows 11만 권장하기 때문이다). 네이티브 PowerShell을 쓴다. Windows 설치 프로그램은 기본 내장
  Windows PowerShell 5.1에서 실행돼야 한다.
- **런타임.** Python ≥ 3.11과 표준 라이브러리만 쓴다(`tomllib`에 3.11이 필요하다). 외부 런타임
  의존성은 없다. Node.js는 unlazy 검사기에만 필요하다.
- **쉬운 시작.** OS마다 설치 명령 하나를 제공한다. 모든 오류에 다음에 실행할 명령을 적는다.
  모든 선택 기능은 사용자가 켜야 한다. 키트 캐시 스킬 업데이트는 안내형 기본값에 포함하며 끌 수 있다(A15).
- **개인정보.** 저장소에는 개인 경로, 이름, 이메일, 제품 이름, 키, 볼트 자료가 없다
  (10절).
- **라이선스.** 키트는 MIT이며 저작권자는 "open-agent-harness contributors"다.
  외부 구성 요소는 연결하거나 의존한다. 스킬은 라이선스가 재배포를 허락할 때만 원본 그대로 번들하고,
  LICENSE, 고정한 업스트림 커밋, 고지를 함께 둔다(9절, A13).
- **비밀값.** 자격 증명을 저장소에 넣지 않는다. 키는 실행할 때만 환경 변수나
  OS 키체인에서 읽는다(A17).
- **CI 비용.** 테스트는 pull request에서만 실행하며 macOS 작업 하나와 Windows 작업 하나를 쓴다(8절).
  별도 Linux 비밀값 스캔 작업은 모든 pull request와 push에서 실행한다(A17).

---

## 4. 원본 목록과 분류

원본은 286개 파일, 7.7 MB다. 주 CLI는 10,635줄짜리 Python 파일 하나이며 하위 명령 33개와
보조 모듈 39개가 있다. 모듈 가운데 12개는 Orca 전용, 11개는 OpenCode 전용이다. 계약
테스트 69개, 최상위 규칙 문서 약 180 KB, 규칙 조각 약 130 KB도 있다. 측정
명령은 14절에 있다.

판정은 **유지**(아이디어를 이식하되 이식 가능하게 다시 작성), **간소화**(초보자가 같은 결과를 얻도록
더 작은 버전으로 이식), **선택**(계정, 유료 서비스, 다른 앱이 필요하면 제공하되 기본으로 끔),
**나중에**(v0.1.0 이후), **제외**(비공개·소유자 전용 내용이나 다른 사람이 쓸 수 없는 것)다.

**2026-10-23에 재판정했다(개정 3).** 모든 행에 한 가지 기준을 적용했다. 초보자가 소유자의
작업 환경을 자동으로 얻는가? 비공개·소유자 전용 내용(이름, 경로, 제품, 계정, 볼트 자료)과
다른 사람이 쓸 수 없는 것만 제외한다. 무겁다는 이유만으로는 더 이상 행을 빼지 않고 좋은 기본값을 갖춰
제공한다. 아래에서 2, 3, 7, 8, 9, 11, 17, 18, 21, 27–35, 37행을 바꾸거나 이유를 추가했다.

| # | 원본 구성 요소 | 원본에서 하는 일 | 판정 | 키트에서 하는 일 |
|---|---|---|---|---|
| 1 | 사용자별 상태 루트의 JSON 원장을 쓰는 리스 핵심 명령 `start`, `claim`, `finish`, `release`, `status`, `log`, `context-save/show` | 병렬 에이전트의 충돌을 막는 공유 잠금과 작업 메모, 이벤트 로그 | **유지** | `oah start/renew/status/finish/release/log`. 작업마다 리스 기록 하나, git 공통 디렉터리 아래 상태, 실제 경로 접두사 겹침 검사, 이식 가능한 잠금(5.4) |
| 2 | 강화한 상태 게시. `renameatx_np`/`renameat2`의 inode 교환, 하드링크로 최초 게시, `unlinkat` 격리, `O_NOFOLLOW` | 심볼릭 링크와 하드링크 경합으로부터 원장 보호 | **간소화(이식 가능)** | 임시 파일 + fsync + `os.replace`와 Windows 재시도(5.4)로, 신뢰한다고 전제하는 사용자 관리 상태 폴더에서 원자적이고 영속적인 게시를 제공한다. 상위 경로를 돌리거나 링크 경합을 일으킬 수 있는 로컬 공격자를 막지는 않는다 |
| 3 | Git 게이트 `mah git`(허용 목록 세 개, 워커는 push·병합 금지) | 에이전트 push를 기계적으로 막음 | **유지, 선택형 훅으로 제공** | 규칙 블록은 워커에게 push를 금지한다. 선택형 `pre-push` 훅은 끝나지 않은 리스가 있으면 거절하고, gitleaks가 설치돼 있으면 실행한다(P5, A17) |
| 4 | 두 리스 역할(구현자와 반영 세션), 단일 병합/push 절차(`READY_FOR_*` 상태) | 한 세션만 반영함 | **간소화** | 규칙은 하나다. 워커는 push하지 않는다. 사용자(리드)가 `oah finish` 뒤 push한다. PR + CI가 결정한다 |
| 5 | 안내문을 주입하는 워커 호출 `mah call`(Claude 또는 Codex) | 프로토콜을 붙여 다른 엔진을 비대화형으로 실행 | **유지** | `oah call <engine> --stage <s>`. 프롬프트는 stdin으로 전달하고 OS별로 실행 파일을 찾으며 시간 초과 때 프로세스 트리를 종료한다(5.5) |
| 6 | 단계 라우팅(`initial→claude`, `review/correction→codex`)과 kind/intensity/reasoning/risk 점수 → 프로필 → 모델/추론 강도 | 단계로 엔진, 난이도로 모델과 추론 강도를 정함 | **유지, 축소** | `.oah/config.toml`의 `[stages]`, `[engines.*.tiers]`, 그리고 `call`과 `packet new`가 자동 적용하는 `oah recommend`(A10). 모델 ID를 코드에 고정하지 않는다(5.5) |
| 7 | TTL 갱신 모델 카탈로그(오래되면 실행 거절) | 모델 목록을 최신으로 유지 | **유지(A14)** | 실행 전마다 읽고 CLI 버전이 바뀌거나 TTL이 지나면 갱신하는 엔진별 모델 카탈로그. tier는 설정한 품질 순서와 CLI 기본값 규칙을 따른다. 변경은 기록하며 명시적 tier 핀이 우선한다(A14) |
| 8 | Orca 배정. 배정기의 실행 부분, 실행 매트릭스, 하트비트, 오케스트레이션 게이트, 반영 큐(모듈 12개) | Orca 앱 안에서 감독하는 워커 | **선택** | Orca가 설치돼 있으면 `oah call`에 선택형 Orca 어댑터를 쓴다(레인 P3o). 기본값은 일반 CLI다 |
| 9 | tier 서브에이전트(Claude 에이전트 파일 5개, tier 표, tier 동기화, PreToolUse tier 가드) | 난이도에 따라 서브에이전트별 모델 + 추론 강도 설정 | **유지, tier 가드는 나중에** | `oah init`이 `.claude/agents/oah-{low,medium,high,max}.md`를 쓴다. `oah tiers sync/check`를 제공한다. 선택형 tier 가드는 v0.1.0 이후로 미룬다 |
| 10 | 규칙 블록 동기화(공유 규칙 원본, CLAUDE.md/AGENTS.md의 마커 블록, AGENTS.md 바이트 제한) | 모든 엔진 지침 파일에 같은 규칙 유지 | **유지** | `oah rules sync/check`. `AGENTS.md`에 블록 하나를 두고 `CLAUDE.md`가 `@AGENTS.md`로 가져온다. 전역 파일은 건드리지 않는다(5.11) |
| 11 | 세션 연속성(진입 프롬프트 생성기, SessionStart 훅) | 새 세션이 이전 세션이 멈춘 곳에서 재개 | **유지** | `oah handoff`와 선택형 SessionStart 훅. 활성 리스, 최근 로그 항목, 최신 인계문을 출력하며 `OAH_WORKER=1`이면 아무것도 출력하지 않는다(P7) |
| 12 | 작업 배정 패킷(`mah packet`, 워커 브리프 템플릿) | 의도, 범위, 사전 점검, 게이트, 보고 형식을 한 파일에 보관 | **유지** | `oah packet new/check` → `.oah/tasks/<task>/PACKET.md`(5.6) |
| 13 | 원래 의도 블록(`MAH_INTENT`. id, revision, status, 여섯 절, sha256 다이제스트, 조건별 `AC<n>: met/unmet/deferred` 줄) | 구현, 리뷰, 수정 내내 목표를 바이트 그대로 유지 | **유지** | `OAH_INTENT` 블록, 같은 다이제스트 규칙(CRLF→LF), `oah intent show/check`(5.6) |
| 14 | 사전 점검 스크립트. 오래된 기준점, 활성 잠금, 최근 커밋, 변경된 작업 트리, 마이그레이션 번호 검사, 수정 파일의 영향. ABORT / PROCEED_WITH_WARNINGS / PROCEED | 오래되거나 다른 작업과 겹치는 기준점에서 작업을 막음 | **유지, Python으로 이식** | `oah preflight`. 일반 검사를 제공하고 마이그레이션 번호 같은 프로젝트별 검사는 설정 명령으로 둔다(5.7) |
| 15 | unlazy 게이트 원장. 미충족 게이트가 있으면 `finish` 거절, 격리 재실행, Stop 훅 라우팅, 정적 원장 린트 | "완료"를 주장으로 끝내지 않고 증거로 확인 | **유지** | unlazy가 검사를 실행한다. `oah`는 읽기 전용 상태 파서, `finish` 가드, 리스를 인식하는 Stop 훅을 제공한다(5.8) |
| 16 | 바뀐 만큼만 검증하는 규칙. 바뀐 파일에 결속한 게이트만 재실행하고 무거운 게이트는 최종 head에서 한 번 실행 | 엄밀함을 지키며 리뷰 라운드 비용 절감 | **유지** | 규칙 블록의 문구와 `oah impact --changed-since` |
| 17 | 영향 지도. 어휘 기반 `git grep`, 게이트 CHECK 결속(pin/exec), 제품 DB 백엔드, 자동 갱신 워크트리, 외부 재정렬 계층 | "X를 바꾸면 무엇을 다시 검사해야 하는가?" | **유지(A11)** | `oah impact`를 저장하고 최신으로 유지한다. `oah init`이 만들고 커밋, 병합, 작업 단계에서 증분 갱신하며 최신 상태를 식별할 정보를 기록한다. 정확한 게이트(`gates#G3`), 어휘·위키 참조, 동시 변경 이력, 선택형 그래프 이웃을 제시한다. 기본 SQL 플러그인이 DB 영향 그래프를 제공한다. 선택형 재정렬기는 순서를 바꿀 수 있지만 결정적 행을 제거하지 않으며 `used` 또는 `unused(reason)`을 보고한다(5.10) |
| 18 | 편집 시점 영향 힌트 훅 | 위험한 파일을 편집하기 전에 결속을 보여 줌 | **유지, 선택형 훅(A11)** | 선택형 Claude Code 훅이 위험한 파일을 편집하기 전에 영향 결속을 출력한다(P7) |
| 19 | graphify 래퍼(report/query/path/explain/update, 안전하지 않은 곳에서는 update 차단) | 지식 그래프에 안전하게 접근 | **유지, 얇게 구현** | graphify가 설치돼 있으면 `oah graph …` 제공 |
| 20 | `netwaif/knot`에서 착안한 위키 감사(깨진 위키링크, 미색인, 출처 누락, frontmatter 없음, 고립, 중복, 오래됨) | 위키 정리 상태 검사 | **유지** | 자체 코드인 `oah wiki lint`(knot에는 라이선스가 없으므로 복사하지 않는다) |
| 21 | 위키 관례. 목록 색인, 삽입 마커가 있는 최신순 로그, 모든 노트의 출처 줄, 위키링크 | 에이전트가 믿을 수 있는 장기 프로젝트 기억 | **유지(A12)** | `oah init`이 Obsidian 호환 `docs/wiki/`의 뼈대를 만든다. 리드나 안내형 기본 절차가 최종 커밋, 게이트, 리뷰 전에 `oah wiki maintain`을 실행하고 `finish`는 유지 관리 여부만 검증한다(A12) |
| 22 | 리뷰 루프. 호출 시 라운드 집계, 시리즈 키, 시리즈별 잠금, 명시적 추가 라운드 허용 | 수렴하는 리뷰 반복 | **유지, 변경(A22)** | 고정 상한 없이 판단이 필요하면 멈추는 `oah review`, 선택형 `[review] max_rounds`, `--extra-round`(5.9) |
| 23 | 리뷰 산출물 스키마와 검증(판정/지적 일관성, base/head 재출력) | 기계로 검사할 수 있는 리뷰 결과 | **유지, 축소** | `codex exec --output-schema`에 전달하는 JSON 스키마, 라운드마다 영수증 하나 |
| 24 | 카나리 토큰(호출마다 무작위로 만들어 리뷰어 트리에 쓰며 프롬프트에 넣지 않음, 증거에 나타나야 함) | 스냅샷의 토큰 파일에 접근했다는 것만 증명 | **유지** | 같은 장치(5.9) |
| 25 | 정제한 리뷰 스냅샷(정확한 head의 새 클론, 에이전트 지침 파일 제거, 리뷰어 설정·규칙 무시, 최소 환경) | 저장소 파일의 자동 지침 로딩을 줄임 | **유지** | `CLAUDE.md`와 `.claude/`도 제거하는 이식 가능한 Python 버전(5.9) |
| 26 | "같은 엔진은 독립적이지 않다" 규칙 | 정직한 신뢰 표시 | **유지, 표시로 제공** | 영수증 필드 `independent: true/false`와 눈에 보이는 경고 |
| 27 | 리뷰어 실행 파일 고정(해시 + macOS 코드 서명 + 로그인 방식 요구) | 리뷰어 실행 파일 신뢰 | **간소화** | CLI 경로, 버전, sha256을 기록하고 고정은 선택 사항으로 둔다. 재판정 이유는 코드 서명 검사가 macOS에만 있고 로그인 방식은 계정 선택의 문제이기 때문이다 |
| 28 | 다섯 레인 협력 검사(목표, QA, 품질, 보안, 문맥) | 최종 head에서 자체 점검할 축 | **간소화** | 다섯 축(목표, 테스트, 품질, 보안, 문맥)을 리뷰 기준과 `oah review summary`의 체크리스트로 둔다 |
| 29 | 반영 감사. CI check-run에 결속한 리뷰 묶음, 간소 감사와 중요 감사, 반영 종료 영수증, `verify-landing` | 병합 전 최종 감사 | **유지, 축소** | `oah land --check`가 수렴한 리뷰 영수증, CI check-run(`gh`가 있으면 이를 사용), 스캔, 게이트가 모두 정확한 head에 결속했는지 검증한다. 이어 기본 브랜치 영향을 갱신하고 새로 영향받는 검사를 push나 병합 전에 요구한다(5.9). `--merge`는 사용자 확인 뒤에만 반영한다(P6) |
| 30 | 스킬 장치. suggest/pack/route/audit/telemetry/lock, 자동 유지 관리, 로컬 패치를 더한 외부 스킬 번들 | 스킬 관리·주입 | **유지, 범용화(A9, A13, A15)** | 브리프별 스킬 선택, 라이선스가 허락하는 범위에서 출처와 함께 스킬 번들, 자동 업데이트. 원본 장치는 복사하지 않고 선택기는 새 코드로 만든다 |
| 31 | 외부 AI 재정렬 서비스 계층(난이도 보조 판정, 모델 업그레이드 보조 판정, 푸터) | 선택형 AI 재정렬 | **선택(A9, A10)** | 재정렬기 플러그인 인터페이스. 첫 제공자인 TypeSafe Jev는 명시적으로 켜고 키가 있을 때만 실행한다. 난이도 판정은 참고용이다(A9) |
| 32 | 실험용 제3 엔진과 무료 모델 변형(OpenCode, Spark, 보조 실행) | 실험 | **나중에, 선택** | v0.1.0 이후 선택형 OpenCode 엔진 어댑터. 소유자의 기본 라우팅에는 속하지 않는다. 제공자별 무료 모델 변형은 제외한다 |
| 33 | 사용 종료 엔진(Antigravity, Grok, Kimi) | 과거 라우팅 대상 | **제외** | — (원본에서도 사용 종료) |
| 34 | 웹 정책. 모든 웹 작업을 Aside로 보내고 내장 웹 도구 차단 | 소유자의 도구 선택 | **선택** | Aside가 설치돼 있으면 `oah init`이 웹 규칙(웹 작업을 Aside로 보냄)을 제안한다. 없으면 엔진 내장 웹 도구를 유지한다 |
| 35 | 소유자 운영 스크립트(고정 경로 pre-push 검사, CI·레인 감시기, 과거 감사 레인 두 개, 세션 문맥) | 일상 운영 | **간소화** | pre-push 비밀값 스캔을 선택형 훅으로 제공한다(A17). 나머지는 소유자의 고정 경로와 레인을 쓴다 |
| 36 | 계약 테스트 69개 | 원본 보호 | **제외** | 테스트를 처음부터 새로 쓴다(8절) |
| 37 | 긴 규칙 문서(공유 규칙 약 94 KB, 라우팅 정책 약 47 KB, 리뷰 절차, 규칙 조각) | 운영 설명서 | **유지, 다시 작성** | 영어와 한국어 초보자 문서로 다시 쓴다(P9) |

**절대 복사하면 안 되는 원본의 비공개 데이터.** 57개 파일에 소유자의 절대 홈 경로가 있다.
47개에는 소유자의 제품 이름이, 23개에는 키체인 항목 언급이 있다(14절). 한 테스트 픽스처에만
홈 경로가 들어간 줄이 681개다. 파일 단위로 그대로 복사하지 않는다. 키트의 모든 파일을 새로 쓰고
매 push 전에 10절의 스캔을 실행한다.

**원본에서 찾은 불일치**는 이식하지 않는다.
- 문서에는 엄밀성 tier가 3개 레인이라고 돼 있지만 검증기는 5개 레인을 요구한다.
- 한 곳에는 "R4는 사용자 승인이 필요하다"고 하고 다른 곳에는 "라운드별 승인은 없다"고 한다.
- 문서에는 `finish`가 unlazy CLI를 쓴다고 하지만 코드는 라이브러리 파서를 호출한다.
- 스냅샷 정제에서 `AGENTS.md`는 제거하지만 `CLAUDE.md`는 제거하지 않는다.

---

## 5. 목표 설계

### 5.0 전체 과정을 이끄는 주체

- **리드는 대화형 Claude Code 세션**, 또는 터미널의 사용자다.
  - Claude Code의 일반 권한 확인을 받으며 리스 아래에서 직접 1차 구현을 한다.
  - 셸 도구로 `oah`의 `preflight`, `start`, `wiki maintain`, `gates check`, `review`,
    `fix`, `finish`를 실행한다. 추적 파일의 유지 관리는 최종 커밋, 게이트, 리뷰 전에 한다(5.10).
  - 사용자는 요청받은 실행을 승인한다.
- **Codex는 워커로 실행한다.** `oah review`와 `oah fix`가 띄운다(`codex exec`, 비대화형).
- **`oah call claude`**(헤드리스 `claude -p`)는 두 번째 레인이나 Codex가 이끄는 세션에 쓴다.
  기본값은 `--permission-mode acceptEdits`이며 설정의 `[engines.claude]`에 있는 도구 허용 목록을 쓴다.
  `bypassPermissions`를 기본값으로 쓰지 않는다.
- **긴 실행.** `oah review|fix|call --background`는 곧바로 실행 ID를 반환한다. `oah wait <run-id>`는
  최대 9분 기다리며 다시 호출할 수 있다. 약 10분 뒤 명령을 멈추는 에이전트 셸 도구에 맞춘 것이다.
- **Codex가 이끄는 세션**도 설정의 단계를 바꾸면 같은 방식으로 동작한다. `workspace-write`
  샌드박스는 `.git`에 쓸 수 없으므로 `oah`의 상태 쓰기를 승인하거나 `OAH_STATE_DIR`을 설정해야 한다.

### 5.1 `oah init`이 프로젝트에 만드는 것

```text
your-project/
  AGENTS.md              # gets one managed block: <!-- OAH:RULES:BEGIN v1 --> … <!-- OAH:RULES:END -->
  CLAUDE.md              # gets one managed line: @AGENTS.md (Claude Code imports it)
  .oah/
    config.toml          # stages, engine tiers, lease TTL, review and preflight settings
    tasks/<task>/        # one folder per task, committed with the work
      PACKET.md          #   intent + scope + plan + acceptance + return format
      GATES.md           #   unlazy-format gate ledger (git-ignored by default, see 5.8)
      GATES.summary.md   #   path-free summary written by `oah gates check` before commit and review
    templates/           # packet, gates and wiki-note templates the commands copy from
  docs/wiki/
    index.md             # catalog by category
    log.md               # newest first, under <!-- OAH:LOG_INSERT_BELOW -->
    log_archive_NN.md    # older whole log entries, moved by `oah wiki rollover`, never edited
    decisions/           # decision records (ADR style)
    raw/                 # optional: imported source documents, append-only
  .claude/agents/        # optional: oah-low/medium/high/max.md (model + effort per tier)
  .graphifyignore        # optional: sensible defaults when graphify is used
```

런타임 상태(리스, 이벤트, 실행 로그, 리뷰 영수증)는 **작업 트리 밖**인
`<git common dir>/oah/`에 둔다. 저장소의 모든 워크트리가 함께 본다. `git status`에는 나타나지 않고
클론을 삭제하면 함께 삭제된다. `OAH_STATE_DIR`로 위치를 바꿀 수 있다.

### 5.2 키트 저장소

```text
open-agent-harness/
  README.md, README.ko.md          # quick start (English, Korean)
  LICENSE, THIRD_PARTY_NOTICES.md
  pyproject.toml                   # distribution "open-agent-harness", console script "oah"
  src/oah/
    cli.py                         # argparse entry, grouped help
    paths.py  lockfile.py  atomic.py  state.py  lease.py  events.py
    config.py  routing.py  engines/{claude.py,codex.py,proc.py}  call.py
    packet.py  intent.py  preflight.py  gates.py  hooks.py
    review/{snapshot.py,canary.py,schema.py,receipts.py,rounds.py}
    wiki.py  graph.py  impact.py  rules.py  tiers.py  init.py  doctor.py
    models.py  secrets.py  skillsel.py  rerank/  setup/  handoff.py  land.py   # amendment 3
    templates/                     # rules block (en, ko), packet, gates, wiki, agents, config
  install/install.sh  install/install.ps1
  docs/  PLAN.md  install-macos.md  install-windows.md  concepts.md  example.md  (+ ko/)
  examples/hello-oah/              # tiny Python project for the worked example
  skills/                          # vendored skills, unchanged, each with its LICENSE and upstream pin (A13)
  mcp/                             # MCP launch templates for Claude and Codex, ${ENV_VAR} placeholders only (A13)
  docs/UPSTREAM_SYNC.md            # ports from the owner's harness, reviewed commit by commit (A16)
  tests/                           # stdlib unittest; fake engines; temp repos
  tools/privacy_scan.py            # this repository's own pre-push scan
  .github/workflows/ci.yml         # added only after the owner approves CI (section 8)
```

### 5.3 런타임과 패키징

- **언어는 Python 3.11+이며 표준 라이브러리만 쓴다.** 원본 로직은 Python이고 graphify에도 이미
  Python이 필요하다. `tomllib`로 설정을 읽는다. 외부 런타임 의존성이 없으면 설치가 짧고 감사할 것도 없다.
- **`uv tool install`로 설치한다.** uv는 Python이 없으면 직접 설치하고 두 OS 모두에서 `oah`를
  PATH에 넣는다. 대안은 `pipx install`과 `python -m pip install --user`이며 PATH 안내를 붙인다.
  저장소가 비공개인 동안에는 클론에서 설치한다. 공개 뒤에는 부트스트랩으로 git 없이 검증한
  고정 릴리스 압축 파일에서 설치하고(6.5), 나중에는 PyPI에서 설치한다. PyPI의
  `open-agent-harness` 이름은 비어 있다. `oah`는 관련 없는 프로젝트가 이미 쓰고 있으므로
  명령만 `oah`라고 부른다(13절).
- **모든 코드의 이식성 규칙.**
  - `shell=True`를 쓰지 않는다. Windows의 32K 명령줄 제한을 피하도록 프롬프트는 stdin으로 보낸다.
    `shell=False`만으로는 Windows 배치 파일 인자 주입을 막지 못한다.
  - `shutil.which`로 실행 파일을 찾고 `.exe`를 우선한다. `.cmd`나 `.bat`를 직접 실행하지 않는다.
    인식한 npm shim은 설치된 패키지가 선언한 bin 스크립트를 찾아 shim 텍스트를 평가하지 않고
    `node.exe`와 argv 목록으로 실행한다. 그 밖에는 `OAH_UNSUPPORTED_BATCH_SHIM`으로 거절하고
    네이티브 실행 파일을 설치하거나 명시적인 `[interpreter.exe, script, ...]` 명령을 설정하라고 안내한다.
    경로와 설정한 인자는 문자 그대로 유지한다.
  - 파일을 읽고 쓸 때마다 UTF-8을 명시한다(한국어 Windows 기본값은 cp949다). 파서는
    CRLF를 받아들이고, 쓰기는 대상 파일의 줄바꿈을 유지한다.
  - Windows 분기 없이 `fcntl` 전용, `O_NOFOLLOW` 전용, signal 전용 코드 경로를 두지 않는다.

### 5.4 상태와 리스

- **위치.** `git rev-parse --git-common-dir` + `/oah/`다. 모든 워크트리가 공유한다. Codex의
  `workspace-write` 샌드박스는 `.git`을 읽기 전용으로 만들므로 사용자나 상위 프로세스의
  `oah call`이 실행한 `oah` 자체만 상태를 쓴다. 샌드박스 워커는 상태를 쓰지 않는다.
- **파일.** `.lock`, `leases.json`, `events.jsonl`, `runs/<task>/<run-id>/…`, `reviews/<task>/…`다.
- **잠금.** 상태 디렉터리마다 잠금 파일 하나를 둔다. macOS는 `fcntl.flock`, Windows는 재시도와 대기 간격
  증가를 적용한 `msvcrt.locking`을 쓴다. 임계 구역의 시간은 제한한다. A15 업데이트는
  리스 획득과 직렬화하도록 검증부터 게시까지 잠금을 유지한다.
- **원자적 쓰기.** 같은 디렉터리에 임시 파일을 쓰고 flush, fsync, `os.replace`를 차례로 실행한다.
  Windows에서는 `PermissionError`가 나면 몇 번 재시도한다(백신과 색인기가 잠깐 파일을 잡기 때문이다).
- **리스 기록.**
  `{id, task, agent, scope[], note, worktree, branch, head_at_start, packet?, intent{source,id,revision,digest}?, gates[], created_at, expires_at}`.
- **겹침.** `repo`는 모든 범위와 겹친다. 경로 범위는 정규화하고 대소문자를 접은 저장소 상대 경로
  하나가 다른 경로의 접두사면 겹친다. 원본은 문자열 완전 일치만 비교해서 `src`와
  `src/a.py`가 충돌하지 않았다.
- **범위는 작업 중에는 지침이며 게이트에서 검사한다.** 편집기의 쓰기는 아무것도 가로챌 수 없다.
  따라서 `oah`는 `finish`와 `review` 때, 그리고 `fix` 뒤에 실제 변경(base..head와 작업 트리)을
  리스 범위와 비교하고 범위 밖 파일을 나열한다. 범위를 넓히거나(`oah renew --scope …`)
  `--allow-out-of-scope`를 주기 전까지 `finish`는 거절한다. 두 경우 모두 기록한다.
  `oah wiki maintain`의 위키 유지 관리 출력 가운데 `docs/wiki/log.md`, `docs/wiki/index.md`,
  새로 만든 `docs/wiki/log_archive_NN.md`만 범위 검사에서 제외한다.
  예외마다 작업과 출력 다이제스트를 이벤트에 기록한다. 다른 위키 편집에는 범위가 필요하다.
- **TTL.** 기본값은 4 h다. `oah renew`로 연장한다. `status`는 만료된 리스를 삭제하지 않고
  STALE로 보여 준다. `start`가 정리한다.
- **명령.**
  - `start`는 겹치면 거절하고 보유자를 알려 준다.
  - `finish`는 선언한 게이트와 위키 유지 관리를 검사하고(5.10), 런타임 요약을 기록한 뒤
    리스를 해제한다. 추적 파일에는 아무것도 쓰지 않는다.
  - `release`에는 `--reason`이 필요하고 다른 사람의 리스에는 `--force`도 필요하다. 중단으로 기록한다.

### 5.5 엔진과 라우팅

```toml
# .oah/config.toml (excerpt; created by `oah init`, edit freely)
[stages]
initial    = "claude"   # first implementation
review     = "codex"    # every review round
correction = "codex"    # fixes for review findings

[engines.claude]
command = ["claude"]
quality_order = ["opus", "sonnet", "haiku"]  # strongest first; CLI aliases, editable
[engines.claude.tiers]
low    = { model = "", effort = "low",    timeout_min = 15 }
medium = { model = "", effort = "medium", timeout_min = 30 }
high   = { model = "", effort = "high",   timeout_min = 60 }
max    = { model = "", effort = "max",    timeout_min = 90 }

[engines.codex]
command = ["codex"]
quality_order = []  # default: effective configured model; user may name an ordered list
[engines.codex.tiers]   # model = "" uses A14, then the effective-config fallback below
low    = { model = "", effort = "low",    timeout_min = 15 }
medium = { model = "", effort = "medium", timeout_min = 30 }
high   = { model = "", effort = "high",   timeout_min = 60 }
max    = { model = "", effort = "highest", timeout_min = 90 }  # the highest effort the model supports
```

- **난이도를 tier로 바꾼다.** `oah recommend --kind <k> --intensity <i> --reasoning <r> --risk <r>`를
  쓰거나 `--tier`를 직접 지정한다. 원본의 점수 규칙은 그대로 유지한다.

  | 축 | 값별 점수 |
  |---|---|
  | kind | quick, docs, research → 1 · review, implementation, refactor, broad-audit → 2 · adversarial-review, migration, security, high-risk-review → 3 |
  | intensity | low 0 · medium 1 · high 2 · max 3 |
  | reasoning | low 0 · medium 1 · high 2 · xhigh 3 · max 4 |
  | risk | low 0 · normal 0 · high 2 · critical 4 |

  - **기본 tier.**
    - risk가 critical, reasoning이 max, 또는 합이 ≥ 8이면 **max**다.
    - 그렇지 않고 risk가 high, intensity가 max, 또는 합이 ≥ 6이면 **high**다.
    - 그렇지 않고 intensity가 high, reasoning이 high나 xhigh, 또는 합이 ≥ 3이면 **medium**이다.
    - 나머지는 **low**다.
  - **상향 규칙.**
    - intensity가 medium, high, max이면 tier를 각각 최소 medium, high, max로 올린다.
    - reasoning이 high나 xhigh이면 최소 high, max이면 max로 올린다.
  - **종류별 하한을 마지막에 적용한다.**
    - broad-audit, migration, security, high-risk-review는 최소 high다.
    - research, review, implementation, refactor, adversarial-review는 최소 medium이다.

  엔진이 하나뿐인 사용자는 세 단계를 모두 그 엔진으로 설정한다. `oah`는 같은 엔진의 리뷰가
  독립적이지 않다고 경고한다.
- **단계 강제.** `oah call <engine> --stage <s>`는 엔진이 `stages[s]`와 다르면 거절한다
  (`OAH_STAGE_ENGINE_MISMATCH`). `--override-stage`를 쓸 수 있으며 이벤트에 기록한다. 리뷰는
  항상 읽기 전용이다.
- **명령줄.** 어댑터이며 2026-10-23에 공식 CLI 참조 문서와 대조했다.
  - Codex: `codex exec -C <dir> -s read-only|workspace-write [-m M] [-c model_reasoning_effort="E"] [--output-schema F] -o <out> [--ephemeral] -`. 프롬프트는 stdin으로 보낸다.
  - Claude: `claude -p --output-format json [--model M] [--effort E] --permission-mode <mode> [--tools …]`. 프롬프트는 stdin으로 보낸다. `--tools`는 사용할 수 있는 도구 자체를 제한하고 `--allowedTools`는 사전 승인만 한다.
- **추론 강도는 모델이 지원하는 값에서 정하며 고정 상한이 없다.**
  - tier의 model이 비어 있지 않으면 명시적 핀이며 항상 우선한다. 아니면 A14의 자동
    카탈로그 매핑을 쓰고, 그다음으로 선택한 프로필을 포함해 실행 디렉터리에 적용되는 엔진의
    실제 설정을 쓴다. Codex 어댑터는 구체적으로 정한 모델을 `-m`으로 전달하고 기록한다.
    새 설치를 포함해 구체적인 기본 모델을 정할 수 없으면 실행 전에 `OAH_MODEL_UNRESOLVED`로
    거절하고 `.oah/config.toml`에 tier의 `model`을 명시하라고 안내한다.
  - `oah`는 `$CODEX_HOME/models_cache.json`에서 그 모델의 `supported_reasoning_levels`를 읽는다.
  - 쓸 수 있는 메타데이터가 있으면 tier에 직접 적은 추론 강도가 그 목록에 있어야 하며, 없으면
    `OAH_EFFORT_UNSUPPORTED`다.
  - `highest`는 목록에서 가장 높은 추론 강도로 정한다. 작업 위임을 추가하는 `ultra`처럼
    동작을 바꾸는 모드는 건너뛴다. 2026-10-23의 `gpt-6.1-sol`에서는 `max`다.
  - 원본의 옛 "max → xhigh" 상한은 실측 결과 낡은 규칙이었다. Codex CLI와 Orca 1.4.237은
    `gpt-6.1-sol`의 `max`를 받아들인다.
  - 캐시가 없거나 해당 모델에 쓸 수 있는 항목이 없으면 직접 적은 강도를 그대로 아래 검사에
    전달한다. 일반 추론 단계를 읽을 수 없을 때 `highest`는 실행 전에
    `OAH_EFFORT_METADATA_MISSING`으로 거절한다. 메시지는 Codex 모델 메타데이터를 갱신하거나
    명시적 모델과 구체적인 tier 강도를 설정하라고 안내한다. 최댓값을 추측하지 않는다.
  - 실행기가 요청한 강도를 실행할 수 없으면 `oah`는 `OAH_SEPARATE_SESSION_REQUIRED`로 멈추고
    별도 세션의 정확한 실행 명령을 출력한다.
- **실행 설정과 추론 강도는 따로 검사한다.**
  - `requested_effort`는 tier 값(`highest` 포함)이고 `launch_effort`는 `highest`를 해석한 뒤
    Codex에 전달하는 구체적인 값이다. `header_effort`는 헤더의 `reasoning effort: <e>`이며
    실제 사용한 추론 강도의 증명이 아니라 `config.model_reasoning_effort`를 보고한다.
  - `oah`는 `header_effort`와 `launch_effort`를 비교한다. 더 낮은 설정은
    `OAH_EFFORT_DOWNGRADED`, 그 밖의 불일치는 `OAH_EFFORT_MISMATCH`로 실패한다. 실패한 리뷰
    라운드는 세지 않는다. 읽을 수 없는 헤더는 일치가 아니라 `unverified`로 기록한다.
  - 어댑터는 기록한 CLI 버전과 모델에 대해 검증한 정규화 규칙으로 `launch_effort`와
    `header_effort`를 각각 추론 강도에 매핑한다. 예를 들어 `ultra`는 다른 추론 강도로
    정규화될 수 있으므로 그 이름들을 순서 척도로 비교해서는 안 된다.
    `resolved_inference_effort`는 헤더에서 도출한 값을 저장하고 `normalization_basis`는 쓴 규칙을
    식별한다. 정규화한 실행 강도보다 낮은 값이면 `OAH_EFFORT_DOWNGRADED`로 실패한다.
  - 정규화 규칙을 모르면 `resolved_inference_effort: unverified`로 기록한다. 일치하는 헤더는
    실행한 설정만 증명한다. 알려진 매핑은 예상 추론 동작을 설명할 뿐 런타임 관측은 아니다.
    헤더가 일치하고 정규화 규칙도 알아야 추론 강도를 검증한 리뷰로 센다. `oah`는 강도를 알리지 않고 낮추지 않는다.
  - Claude에서는 P3가 추론 강도를 관측할 수 있는지 검증한다. 그전까지는 미검증으로 기록한다.
- **실행.** 호출마다 `prompt.md`, `stdout.log`, `stderr.log`, `result.json`을 쓴다. 결과에는 종료 코드,
  소요 시간, CLI 버전, 정한 모델, `requested_effort`, `launch_effort`, `header_effort`,
  헤더 검사 상태, `resolved_inference_effort`, `normalization_basis`를 담는다. 알 수 없는 값은
  `unverified`다. 사전 안내문은 짧은 템플릿으로, 작업, 단계, 범위, 규칙(범위 준수, push 금지,
  원장에 증거 기록), 패킷, 보고 형식을 담는다.
- **배정한 워커임을 표시한다.**
  - `oah`가 띄우는 모든 워커의 환경에 `OAH_WORKER=1`, `OAH_RUN_ID`, `OAH_TASK`를 넣는다.
  - 사전 안내문의 첫 줄에 배정받은 워커라고 밝힌다.
  - 워커는 모든 세션 진입 절차를 건너뛴다(5.11, 5.12).
  - 워커는 배정자가 이미 잡은 리스를 쓴다. 리스를 시작·갱신·해제·완료하지 않는다.
    브리프에는 리스 명령 대신 리스 범위와 의도 결속을 담는다. `OAH_WORKER=1`이면
    모든 리스 쓰기를 `OAH_WORKER_LEASE_REFUSED`로 거절한다(C1, 2026-10-24).
  - 1차 구현과 수정 워커는 배정자가 리스를 잡은 워크트리에서만 실행한다. `--worktree`가
    다른 트리를 가리키면 `OAH_WORKTREE_LEASE_MISMATCH`로 거절한다. 읽기 전용 리뷰는 어떤
    트리에서든 실행할 수 있다(C1).

### 5.6 패킷과 의도

`oah packet new <task>`는 `.oah/tasks/<task>/PACKET.md`를 만든다.

```markdown
<!-- OAH_INTENT:BEGIN -->
## Intent
Intent-ID: <task>
Revision: 1
Status: draft            # becomes "accepted" when you approve it
### Problem
### Desired outcome
### Success conditions
- AC1: <checkable condition>
### Constraints
### Non-goals
### Open questions
<!-- OAH_INTENT:END -->
## Scope        (paths this task may write)
## Plan         (stage → engine → tier)
## Acceptance   (GATES.md in this folder)
## Return       (INTENT <id>@r<n> <digest> + one "AC<n>: met|unmet|deferred — evidence" line each;
                rerank=used|unused(<reason>) when impact was used; the DB-impact line for DB changes)
```

- `oah packet check`는 필수 절, 고유 AC ID, `start --packet` 전의 `Status: accepted`,
  원장 파싱 여부를 검증한다.
- **다이제스트.** BEGIN부터 END까지의 바이트를 CRLF→LF로 바꾸고 끝에 LF 하나를 둔 뒤
  sha256을 구한다. 의도를 바꿀 때는 제자리 편집이 아니라 새 개정으로 만든다.
- **결속.** 리스는 `{source, id, revision, digest}`를 기록한다. 리뷰 프롬프트에 의도를
  그대로 담고 영수증에 다이제스트를 기록한다. 불일치는 보고한다.
- **의도 계보(A7).** 하나의 절이 모든 단계에 목표를 전달한다.
  - **의도 원본은 하나다.** 패킷의 `OAH_INTENT` 절만이 패킷 작성, 구현, 리뷰, 수정의 모든 단계에서
    목표를 서술한다. 리드, 워커, 서브에이전트 모두에 적용한다.
    - 리뷰와 수정 브리프는 같은 절을 바이트 그대로 담는다.
    - 라운드 지시는 그 절 밖에 둔다.
    - 후속 라운드는 첫 배정 때 결속한 의도 원본을 다시 쓴다. 브리프의 원본이 결속과 다르면
      `OAH_INTENT_SOURCE_MISMATCH`로 거절한다. 목표를 바꾸려면 새 개정이 필요하다(A19).
  - **새 ID 대신 개정을 올린다.** 목표가 바뀌면 같은 `Intent-ID`의 `Revision`을 올리고
    다시 `Status: accepted`를 받아야 한다. 라운드마다 새 ID를 만들지 않는다.
  - **결과만 적는다.** 성공 조건은 결과를 설명한다. 리스, 커밋, 리뷰 라운드, 보고 형식 같은
    절차 단계는 그 절 밖에 둔다. 조건이 절차 단계처럼 읽히면 `oah packet check`가
    경고한다(`OAH_INTENT_PROCESS_STEP`).
  - **같은 start 명령에서 결속한다.**
    - `oah start --packet <PACKET.md>`가 리스를 `{source, id, revision, digest}`에 결속한다.
      배정자가 실행하며 배정받은 워커는 실행하지 않는다(C1).
    - `oah status`와 `oah intent show`는 아래에서 처음 맞는 조건을 쓴다.
      - `unbound`: 리스에 의도 결속이 없다.
      - `missing`: 결속은 있지만 그 의도 원본이 없다.
      - `current`: 원본을 읽을 수 있고, 올바른 `OAH_INTENT` 마커 쌍과 절이 정확히 하나 있으며,
        `Status: accepted`, 결속한 ID와 개정, 위 정규화를 적용한 다이제스트가
        모두 결속과 같다.
      - `stale`: 결속과 원본은 있지만 `current` 검사 가운데 하나라도 실패한다. 읽을 수 없는
        내용, 누락·손상·중복 마커, 잘못된 필드, 승인되지 않은 상태, 변경된 식별 정보나
        다이제스트가 포함된다. 잘못된 내용은 절대 `current`일 수 없다.
    - `oah call`, `oah review`, `oah fix`는 의도가 `current`가 아닌 작업의 배정을
      거절한다(`OAH_INTENT_NOT_CURRENT`).
    - 검사 전에 워크트리 선택자는 정확히 한 저장소로 해석돼야 한다. 모호하거나 해석할 수
      없으면 실행 전에 `OAH_WORKTREE_UNRESOLVED`로 거절하고 바로 실행할 수 있는 재시도
      명령을 준다(A19).
  - **보고.** 리드, 워커, 서브에이전트의 모든 보고에는 `INTENT <id>@r<n> <digest>`와
    조건마다 `AC<n>: met|unmet|deferred — <evidence>` 한 줄이 있어야 한다. `oah finish`와
    `oah review`는 결속과 대조해 구조를 검사한다. 일치하는 `INTENT` 줄이 정확히 하나이고,
    결속한 AC마다 유효한 상태/증거 줄이 하나여야 한다. 누락, 잘못된 형식, 불일치, 중복 줄,
    새로 지어낸 AC는 `OAH_INTENT_RETURN_INVALID`로 거절한다.
  - **공유 푸터 하나.** 하나의 정본 의도 푸터가 이 규칙을 적는다.
    - `oah call`, `oah review`, `oah fix`가 모든 워커 브리프에 붙인다.
    - tier 에이전트 파일의 관리 블록 안에도 넣고 동기화할 때마다 교체한다(A5).
  - **초안은 결속할 수 없다.** `oah packet new`는 `Status: draft`로 뼈대를 만들고,
    상태가 `accepted`가 될 때까지 `oah start --packet`은 `OAH_INTENT_NOT_ACCEPTED`로 거절한다.
- **이식하지 않는 것.** 원본의 패킷 파일 POSIX 소유자·링크 수 검사다. 키트는 일반 파일인지,
  심볼릭 링크가 아닌지, 1 MiB 미만인지, stat과 읽기 사이에 바뀌지 않았는지를 검사한다.
- **빈 Scope를 조용히 넘기지 않는다.**
  - `## Scope`에 경로가 없으면 `oah packet check`가 뚜렷이 경고한다(`OAH_SCOPE_EMPTY`).
  - Scope에 경로를 넣거나 `--scope repo`를 명시하기 전까지 `oah start --packet`은 거절한다.
  - Scope의 파일 종류로 선택하는 모든 기능은 같은 경고를 출력하며 조용히 빈 결과를 선택하지 않는다.
    스킬, 도구, 사전 점검 검사, 게이트 템플릿, 영향 질의가 이에 해당한다.
- **"네트워크 금지"는 좁은 의미다.**
  - 패킷의 "네트워크 금지" 같은 제약은 설치, fetch, 외부 웹 접근을 금지하지만 `oah`에 필요한
    두 가지는 예외다. `oah impact`의 기본 브랜치 자동 갱신(A23)과 사용자가 동의해 켰고 키가
    있는 재정렬기다. 브리프에는 "재정렬기와 `oah impact`의 기본 브랜치 자동 갱신을 제외한
    네트워크 금지"라고 적어 워커가 갱신을 한계로 기록하거나 끄지 않도록 한다.
  - 브리프에서 재정렬기를 지목해 끌 수 있다. 제공자 동의와 키 검사는 여전히 적용한다(5.10).
    어떤 브리프도 기본 브랜치 갱신을 끄지 않는다(A23).

### 5.7 사전 점검

`oah preflight [--task T] [--touch PATH…] [--offline]`은 다음 검사를 실행한다.

1. **기준점의 최신 여부.** 기본 브랜치를 `git fetch`한 뒤 HEAD가 얼마나 뒤처졌는지 센다.
   뒤처졌으면 ABORT와 rebase 명령을 준다. 원격 저장소가 있는데 fetch할 수 없어도
   `--offline`이 없으면 ABORT다. 원격 저장소가 아예 없으면 최신 여부는 적용하지 않고 WARN이다.
2. **변경된 작업 트리.** WARN이다.
3. **다른 작업의 활성 리스와 겹침.** ABORT다. 만료된 리스는 WARN이다.
4. **패킷의 기준점 이후 기본 브랜치의 최근 커밋** 가운데 범위를 건드린 것이 있으면 목록과 함께 WARN을 준다.
5. **`--touch` 경로의 영향.** 결속한 게이트를 정보로 나열한다.
6. **패킷.** `--task`를 줬다면 승인 상태이고 AC가 있어야 한다. 아니면 ABORT다.
7. **프로젝트 검사.** `[preflight].checks`에서 가져온다. 각 검사는 argv 목록이며 종료 코드가
   0이어야 한다. "마이그레이션 번호가 비어 있는가" 같은 검사가 들어가는 자리다.

판정과 종료 코드는 PROCEED(0), PROCEED_WITH_WARNINGS(0, 경고를 읽어야 함), ABORT(2)다.
출력은 항상 이 사전 점검이 증명하지 않은 것의 목록인 "not proven by this preflight"로 끝난다.

### 5.8 게이트(unlazy)

- 형식은 unlazy의 것(`- [ ] G1:` … `CHECK:` / `EXPECT:` / `EVIDENCE:` / `ABANDON:`)이다.
  `oah`는 업스트림 형식 문서를 연결하고 복사하지 않는다.
- `oah gates new <task>`는 `cmd.exe`와 `sh`에서 똑같이 실행되는 CHECK 줄이 있는 템플릿으로
  `.oah/tasks/<task>/GATES.md`를 쓴다. CHECK 줄은 `python`, `node`, `oah check …`를 호출하며
  `grep`이나 파이프를 쓰지 않는다.
- **`oah check exists|contains|absent|json …`.** 게이트 CHECK 줄에 쓰는 작은 이식형 단언이다.
  각각 성공 토큰을 출력하므로 Windows 초보자도 POSIX 도구 없이 게이트를 쓸 수 있다.
- unlazy가 설치돼 있으면 `oah gates status|check|lint`는 unlazy의 `gate-check.mjs` /
  `gate-lint.mjs`에 위임한다. `OAH_UNLAZY_DIR`, 프로젝트 스킬 디렉터리, 사용자 스킬 디렉터리
  순서로 찾는다. 게이트 명령을 실행하기 전에 찾은 사본의 커밋과 스크립트 다이제스트를
  P5가 계약 테스트한 핀과 대조한다. 다르면 그 사본을 실행하거나 다음 사본으로 넘어가지 않고
  거절한다. 메시지에 불일치 내용을 밝히고, 고정한 프로젝트 사본을 설치하는
  `oah init --with-unlazy --force`를 제시하며, 호환되지 않는 `OAH_UNLAZY_DIR` 재정의가
  있으면 지우라고 안내한다. CHECK의 **실행**은 항상 unlazy 자체 승인 절차를 거친다.
  `oah`는 CHECK 줄을 직접 실행하지 않는다.
- **게이트에는 unlazy가 필수이며 따라서 Node도 필수다.** unlazy 상태에 따라 작업의 선언된
  원장이 충족되지 않으면 `oah finish`는 거절한다. `ABANDON:` 줄은 성공이 아닌 필수 인계로 보고한다.
- **unlazy가 없어도** 내장 읽기 전용 파서로 체크박스, 보류된 증거, 포기 항목을 보고할 수 있다.
  결과에는 "structural only"라고 표시한다. `--allow-structural`이 있어야만 `finish`를
  통과시키며, 이벤트에 미검증으로 기록한다.
- **리드가 게이트를 실행한다.** 구현 뒤와 매 `oah fix` 뒤에 `oah gates check`를 실행한다.
  워커는 반복 작업 중 테스트를 실행할 수 있지만 CHECK 명령을 승인하거나 게이트 증거를 기록하지 않는다.
  - `oah`는 런타임 상태에 게이트별 입력 명세와 다이제스트를 증거 다이제스트,
    게이트 정의 다이제스트와 함께 저장한다. 추가와 삭제를 포함해 입력 경로, 바이트, 실행 모드를
    해시한다. 입력은 소스, 테스트, 설정, 도구 버전, 관련 환경 변수를 포함한다. 기본값은 생성된
    게이트 출력을 뺀 모든 추적 파일과 무시되지 않은 미추적 파일이다. 더 좁은 입력 집합은
    명시적으로 선언해야 한다. 알 수 없는 의존성이 있으면 보수적으로 증거를 무효화한다.
  - 정의나 입력이 바뀌면 unlazy가 여전히 충족으로 표시해도 증거는 무효다.
    `gates check`는 영향을 받은 게이트를 unlazy의 `--reverify`로 실행하며 기본 동작인
    충족 게이트 건너뛰기에 의존하지 않는다. 검사 전후 입력을 비교해 도중에 바뀌면 실행을 무효화한다.
  - 영향 없는 게이트에도 이전/새 입력 다이제스트와 원래 증거 다이제스트를 담은 명시적인
    입력 동일 승계 기록이 필요하다. `finish`와 리스를 인식하는 Stop 훅은 작업 트리 변경을 포함한
    최종 입력을 기준으로 오래되거나 누락된 결속을 거절한다. unlazy의 통과 상태만으로는 부족하다.
- **원장 개인정보.** unlazy의 자동 증거에는 해석된 작업 디렉터리가 기록되며 사용자 이름이 들어간 절대 경로다.
  - 따라서 `oah init`은 `.gitignore`에 `.oah/tasks/*/GATES.md`를 추가한다.
  - `oah gates check`는 최종 커밋과 리뷰 전에 경로 없는 `GATES.summary.md`를 쓴다.
    `oah finish`는 현재 게이트 기록과 맞는지만 검증한다. 요약에는 검증한 `G<n>` ID,
    고정 상태 열거값, 제목·CHECK·EXPECT·증거·입력/승계 기록의 sha256 다이제스트만 담는다.
    원래 제목, 명령, 기대값, 경로, 작업 이름, 그 밖의 자유 텍스트를 내보내지 않는다.
    작성기는 이 허용 목록 밖의 필드를 거절한다. 전체 텍스트는 무시된 원장이나 런타임 상태에 둔다.
  - `[gates] commit_ledgers = true`이면 대신 전체 원장을 커밋한다.
- **고정한 계약.** P5는 unlazy 커밋과 스크립트 다이제스트를 고정하고 CLI 계약인 인자,
  출력, 종료 코드를 기록한다. `oah init --with-unlazy`는 그 계약 테스트를 거친 커밋을 설치하고
  두 식별 정보를 모두 검증한다. 버전이 정해지지 않은 업스트림 사본은 설치하지 않는다.
  unlazy는 A15 업데이트에서 제외하는 운영 의존성이다. 새 커밋은 그 후보 커밋에 대해
  어댑터 계약과 게이트 무효화 테스트를 통과하는 명시적 변경으로만 채택한다.
  스텁 테스트만으로는 새 핀을 허용하지 않는다.
- **Stop 훅(선택).** `oah hooks install`은 `oah hook stop`을 실행하는 Claude Code Stop 훅을 추가한다.
  활성 리스가 세션의 워크트리를 관할하면 해당 작업의 원장이 미충족인 동안 훅이 차단한다.
  진행 없는 차단이 N번이면 진행 가드가 풀어 영원히 막히지 않게 한다. 관할하는 리스가 없으면
  같은 핀 검증 뒤에만 unlazy 자체 Stop 훅으로 넘긴다. 기본적으로 훅은
  `.claude/settings.local.json`(미추적)에 쓴다. 명령이 `oah hook stop`뿐이므로
  공유 설치에도 절대 경로가 들어가지 않는다.

### 5.9 리뷰 루프와 신뢰

`oah review --task T [--base REV]`(단계는 `review`, 기본 엔진은 Codex)는 다음 일을 한다.

1. **base와 head 결정.** base는 기본 브랜치와의 merge-base, head는 HEAD다. 둘 다
   전체 SHA로 해석한다. 리뷰 대상이 커밋이 되도록 추적 트리는 깨끗해야 한다. 증거 명령은
   갱신한 기본 브랜치가 아니라 그 커밋과 작업 트리에서 실행한다. 영향은 최신 기본 브랜치를
   읽고 증거는 고정한 HEAD에 둔다(A23).
2. **라운드.** 시리즈 키는 (task, base SHA)다. 완료한 라운드만 센다. 고정 상한은 없다(A22).
   매 라운드의 지적이 새롭고 범위 안에 있으면 계속한다. 리뷰어는 아래의 이전 영수증과
   수정 시도 기록을 보고, 시리즈의 앞선 라운드에서 이미 수정하려 한 유형을 되풀이하는 지적에 표시한다.
   재발 증거가 없거나 모호해도 `OAH_REVIEW_NEEDS_DECISION`으로 멈춘다. 지적 이름만 바꿔도
   유형이 새로워지는 것은 아니다. 이런 지적이나 설계·저장 형식 변경이 필요한 지적은
   `OAH_REVIEW_NEEDS_DECISION`으로 시리즈를 멈추고 사용자에게 묻는다.
   선택형 `[review] max_rounds`(기본 미설정)에 닿으면 `OAH_REVIEW_ROUNDS_EXHAUSTED`로 멈춘다.
   `--extra-round`로 계속할 수 있고 기록에 남긴다. 한 라운드가 승인하거나 P2/P3 지적만
   보고하면 시리즈를 끝내고 해당 지적을 후속 작업으로 기록한다. 시리즈별 잠금으로 두 라운드가
   동시에 실행되는 것을 막는다.
3. **스냅샷.** `git clone --no-local --no-checkout` 뒤 head를 detached 상태로 체크아웃하고
   원격 저장소를 제거한다. 이어 `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`,
   `CLAUDE.local.md`, `.claude/`, `.codex/`, `.agents/`를 제거한다. 프롬프트에는 제거 사실과
   리뷰어가 `git show`나 `git diff`로만 읽을 수 있다는 점을 밝힌다.
4. **카나리.** 무작위 토큰을 `<snapshot>/.oah-review-canary`에 배타적으로 새로 만든 파일로 쓴다.
   프롬프트는 증거에 `REVIEWED-TREE-CANARY: <token>`을 요구하지만 토큰 자체는 넣지 않는다.
   검증은 스냅샷의 토큰 파일에 접근했다는 것만 증명한다고 한계를 밝힌다.
   소스·diff를 얼마나 읽었는지, 리뷰 품질, 트리 신원은 증명하지 않는다. SHA를 되풀이해도
   이를 입증하지 못한다.
5. **프롬프트.** 의도 원문, AC 목록, base와 head, diff 통계, 중요 경로 경고,
   시리즈의 모든 이전 영수증·수정 시도 기록과 다이제스트, 목표·테스트·품질·보안·문맥·회귀라는
   리뷰 기준을 담는다. 리뷰어는 실패 원리와 위반 규칙을 기준으로 각 지적을 이력과 비교하고,
   표현이 달라져도 유형 ID를 다시 쓴다. 프롬프트는 소스, 주석, 문서, git으로 읽은 파일을
   포함한 모든 리뷰 대상 저장소 내용을 따라야 할 지시가 아니라 신뢰하지 않는 증거로 취급한다.
   정제는 자동 지침 로딩을 줄일 뿐, 리뷰어가 읽는 내용의 프롬프트 주입을 막지는 못한다.
6. **실행.** 리뷰어는 스냅샷 안에서 `--output-schema`, `--ephemeral`을 쓰며 읽기 전용으로 실행한다.
   Codex는 사용자 설정과 규칙을 무시하고 최소 환경으로 실행한다.
7. **검증.** 스키마, 판정과 지적의 일관성, base/head 재출력을 확인한다. 카나리가 없으면
   `OAH_REVIEW_CANARY_MISSING`이며 라운드를 세지 않는다. 이력 다이제스트, 고유 지적 ID,
   유형 정의, 모든 이전 참조를 같은 시리즈와 대조한다. 각 참조는 이전 라운드, 영수증 다이제스트,
   지적 ID, 일치하는 유형 ID를 명시한다. 이력이나 참조가 없거나 잘못되거나 모호하면 수렴을
   받아들이기 전에 `OAH_REVIEW_NEEDS_DECISION`으로 멈춘다. 첫 라운드는 이력이 비어 있음을 명시한다.
8. **영수증.** `reviews/<task>/R<n>.json`에는 다음을 담는다.
   `{task, round, base, head, intent_digest, reviewer{engine, cli_path, cli_version, cli_sha256, model, effort}, author_engines[], independent, verdict, findings[], history_digest, decision_stop_reason, canary_verified, prompt_sha256, output_sha256, started_at, finished_at}`.
   영수증마다 `reviews/<task>/index.jsonl`에도 추가한다.
   지적에는 시리즈 전체에서 고유한 `finding_id`, 실패 원리와 위반 규칙의 정의를 갖는
   안정적인 `class_id`, `prior_refs[]`(라운드, 영수증 다이제스트, 지적 ID, 유형 ID)를 보존한다.
   영수증에는 전달한 이력의 다이제스트와 판단을 위해 멈춘 이유도 보존한다.
   수정 시도는 `reviews/<task>/` 아래 별도의 불변 기록으로 두고 다이제스트와 함께 색인한다.
   `{attempt_id, source_receipt_digest, finding_ids[], class_ids[], before_head, after_head,
   outcome, evidence}`다. 결과는 `applied`, `no-change`, `failed`, `aborted`이며 커밋한 결과가
   없으면 `after_head`는 null이다. 이전 기록을 바꾸지 않고 커밋 뒤 head 결속 기록을 덧붙인다.
   모든 시도에 영속적인 시작 기록과 최종 결과가 있어야 한다. 끝나지 않은 시도는 재발 증거를
   불완전하게 만든다. 지적만으로는 수정을 시도했다는 증거가 되지 않는다. 수정하려 한 유형이
   다시 나타나면 판단이 필요하다.

리뷰 뒤에는 다음과 같이 진행한다.
- **`oah fix --task T`**(단계는 `correction`, 기본 엔진은 Codex)는 실제 워크트리에서
  `workspace-write`로 실행한다. 최신 영수증의 지적, 의도, 범위를 받는다. Codex는 그곳에서
  커밋할 수 없으므로(`.git`이 읽기 전용) `oah`가 diff를 보여 주고 사용자가 커밋한다. 다음
  `oah review`는 R(n+1) 라운드다. `oah`는 실행 전에 시도 시작을, 실행 뒤에는 실패·중단을
  포함한 결과를 기록한다. 재리뷰 전에 적용한 결과를 커밋한 head와 증거에 결속한다.
  수동 수정에도 재리뷰 전에 같은 기록이 필요하다.
- **누가 무엇을 썼는가.** 커밋에 `Agent-Engine: claude|codex|human` 트레일러를 넣는다.
  - `oah fix`는 `Agent-Engine: codex`가 포함된 정확한 커밋 명령을 출력한다.
  - 규칙 블록은 Claude에게 `Agent-Engine: claude`를 추가하라고 지시한다. Claude Code 자체의
    `Co-Authored-By: Claude` 트레일러도 인식한다.
  - 둘 다 없는 커밋은 `unknown`이다.
- **`independent`**는 base..head의 모든 커밋에 알려진 작성 엔진이 있고 그것이 리뷰어와 다를 때만
  `true`다. 리뷰어 엔진이 하나라도 썼으면 `false`, 엔진을 모르는 커밋이 하나라도 있으면
  `unknown`이며 절대 `true`가 아니다.
  - 따라서 Codex 수정 뒤 R2에는 "not independent"라고 표시한다.
  - 선택한 엔진 순서를 정직하게 표시한 것이며 오류가 아니다.
- **`oah review summary --task T`**는 PR에 바로 쓸 블록을 출력한다. 라운드, 판정, 영수증
  다이제스트, 독립성 표시, 게이트 상태, 건드린 중요 경로를 담는다. 중요 경로는 설정할 수 있으며
  기본값은 `.github/workflows/**`, 에이전트 지침 파일, `**/migrations/**`다.
- **반영.** push나 병합 직전에 `oah land --check`는 원격의 기본 브랜치를 fetch하고,
  두 영향 지도(5.10)를 써서 가져온 전체 SHA와 작업 HEAD를 기준으로 작업의 변경 경로를 다시 평가한다.
  두 SHA, 영향 결과 다이제스트, 필수 검사를 기록한다. 새로 영향받는 검사는 가져온 게이트 정의와
  결속을 써서 push나 병합 전에 작업 HEAD에서 통과해야 한다. 정확한 head의 리뷰, CI, 스캔,
  게이트라는 기존 요구도 그대로 적용한다. 오프라인 모드, fetch 실패, 영향 확인 불가는
  `OAH_LAND_IMPACT_STALE`로 거절한다. 새로 영향받는 검사가 보류되거나 실패하면
  `OAH_LAND_CHECKS_REQUIRED`로 거절한다. 이전 `--check`는 이후 반영을 허가하지 않는다.
  `--merge`와 리드의 push 절차는 실행 직전에 이 검사를 되풀이한다.
  반영은 리뷰한 정확한 작업 HEAD의 fast-forward push이며, 원격은 기본 브랜치가 fetch한
  SHA와 같아야 한다는 조건을 원자적으로 강제한다. 브랜치가 움직였으면 원격이 거절하고 아무것도
  반영하지 않는다(`OAH_LAND_IMPACT_STALE`). 새 기본 브랜치 SHA를 fetch하고 영향을 다시
  평가하며 영향받는 검사를 다시 요구한 뒤 재시도한다.
- **리뷰어 실행 파일은 기록하며 맹신하지 않는다.** `oah`가 경로, 버전, sha256을 기록한다.
  `[review].codex_sha256`으로 고정할 수 있다. Windows에 적용할 수 없으므로 macOS 코드 서명 검사는 하지 않는다.

### 5.10 지식 — 위키, 그래프, 영향 지도

- **위키(`docs/wiki/`).**
  - 모든 노트에는 frontmatter(`title`, `date`, `sources`, `tags`)와 출처가 적어도 하나 있다.
    출처는 커밋이 붙은 파일 경로, URL, 패킷 가운데 하나다.
  - 노트는 `[[wikilinks]]`로 연결하고 `index.md`에 나열한다. `log.md`에는
    `<!-- OAH:LOG_INSERT_BELOW -->` 아래에 최신순으로 적는다.
  - `oah wiki new "<title>"`은 템플릿에서 노트를 만들고 색인에 추가한다.
    `oah wiki log "<title>"`은 날짜가 있는 항목을 삽입한다.
  - **`oah wiki maintain --task T`**는 최종 커밋, 게이트, 리뷰 전에 추적 로그·색인 유지 관리와
    크기에 따른 로그 분할 보관을 수행한다. 리드가 실행하거나 안내형 기본 절차가 수정 뒤를 포함한
    해당 작업 단계에서 실행한다. 아래의 배타적 위키 잠금과 저널/복구 프로토콜을 쓴다.
    작업 ID로 로그/색인 삽입을 멱등적으로 만들며 분할 보관 뒤에도 같다. 재시도는 기존 항목을
    검증하고 누락된 작업을 중복 없이 마친다. 잠금이 경합하면 제한된 재시도로 기다리다가
    재시도 안내와 함께 거절한다. 미완료 저널이 있으면 명시적 복구가 필요하다.
    성공한 출력 경로와 다이제스트를 5.4의 예외 유지 관리 집합으로 이벤트에 기록한다.
    `finish`는 작업 항목과 유지 관리 기록을 현재 위키 바이트와 대조하고 읽기 전용 린트/정리 상태
    검사만 실행한다. 유지 관리가 없거나 오래됐으면 커밋, 게이트, 리뷰 전에 다시
    `oah wiki maintain --task T`를 실행하라는 메시지로 거절한다. 이 거절을 넘길 수 있는 옵션은
    `--allow-missing-wiki-maintenance`뿐이며 이벤트에 기록한다.
  - `oah wiki lint`는 읽기 전용이다. 깨진 위키링크, 미색인 노트, 출처 누락, frontmatter 누락,
    고립 노트, 중복 제목, 오래된 노트를 텍스트나 JSON으로 보고한다.
  - **자동 정리(A8)는 후보만 제시한다.** 어떤 위키 명령도 스스로 노트를 삭제·병합·이동하지 않는다.
    - **`oah wiki lint`는 실제 문제만 센다.**
      - 에이전트 기억 폴더처럼 설정한 `[wiki] link_roots` 아래에 대상이 있으면 위키 밖의 링크도 해석한다.
      - 인라인 코드와 코드 블록 안의 링크는 무시한다.
      - 보관 파일의 색인도 색인 출처로 센다.
      - 파일 mtime 대신 노트의 마지막 커밋 날짜(`git log -1`)로 오래됐는지 판단한다.
    - **`oah wiki lint --index`**는 `index.md`의 배너, 너무 긴 줄, 실제 미색인 노트를 보고한다.
      펜스로 둘러싼 코드 밖에서 배너는 앞뒤 공백을 제거한 줄이 `^#{1,6} +.*$` 또는
      `^(?:[-=*_] *){3,}$`와 일치하는 경우다. 긴 줄은 `[wiki] index_line_limit`의 유니코드 코드 포인트 수를
      넘는 줄이다(기본값 120, CR/LF 제외). 미색인 노트에는 일반 린트의 색인 출처 규칙을 적용한다.
      후보 목록은 위키 밖의 상태 폴더에 쓴다.
    - **`oah wiki rollover`**는 `log.md`에서 가장 오래된 항목을 통째로 다음 번호의
      `log_archive_NN.md`로 옮긴다.
      - 기본 실행 기준은 정확히 512,000바이트 초과이며, 남길 항목 바이트의 목표 상한은
        409,600바이트다. 실행 기준은 메타데이터를 포함한 전체 파일 크기로 판단한다.
        머리말과 생성한 보관 색인은 목표 크기와 항목 스트림에서 뺀다. 항목 바이트가 이미 목표
        이내인데 메타데이터만으로 기준을 넘으면 아무 일도 하지 않는다.
      - 이 기본값을 갖는 실행 기준과 목표만 `[wiki]` 설정값이다. 보관 이름은 범위 예외,
        린트, 복구가 요구하는 `log_archive_NN.md`로 유지한다(A18).
      - `oah wiki rollover`는 계획을 출력하고 `--apply`가 없으면 쓰지 않는다. `--apply`에는
        `log.md`와 새 보관 파일을 범위에 담는 리스가 필요하며 없으면
        `OAH_WIKI_LEASE_REQUIRED`로 거절한다. 모든 작업이 로그에 추가하므로
        `oah wiki maintain`은 리스 없이 위키 잠금 아래에서 분할 보관을 적용한다(A18).
      - 각 로그·보관 파일의 단 하나인 `<!-- OAH:LOG_INSERT_BELOW -->` 마커 아래에서,
        펜스 코드 밖의 들여쓰기 없는 `## YYYY-MM-DD <title>` 줄이 항목의 시작이다.
        머리말과 `<!-- OAH:ARCHIVE_INDEX:BEGIN -->`, `<!-- OAH:ARCHIVE_INDEX:END -->` 사이의
        생성 표는 그 마커 위에 둔다. 항목은 빈 줄을 포함해 다음 항목이나 EOF 직전까지의 모든
        바이트를 포함한다. `wiki log`는 이 형식으로 출력한다. 마커와 첫 항목 사이의 비어 있지
        않은 내용, 잘못된 날짜, 모호한 경계는 `OAH_WIKI_LOG_INVALID`로 거절한다.
        그 사이의 빈 접두 바이트는 원래 파일에 남긴다. 쓰기 전에 거절하며 메시지에는 마커 누락,
        날짜 없는 첫 항목 제목, 닫히지 않은 펜스처럼 해당 경우를 밝힌다(A18).
      - 각 파일 안에서는 최신순을 유지한다. 최신 항목을 적어도 하나 남기면서 남은 항목 바이트가
        목표 이내가 되도록 가장 오래된 쪽의 최소 연속 묶음을 옮긴다. 최신 항목 하나가 목표보다
        크면 통째로 남기고 크기 초과로 보고한다. 오래된 초과 항목은 통째로 옮긴다.
        옮길 수 있는 항목이 없으면 보관 파일을 만들지 않고 초과로 보고한다. 그렇지 않으면
        목표 크기를 넘더라도 한 번 실행에 보관 파일 하나만 만든다.
      - 보관 번호는 01부터 시작해 기존 최댓값보다 하나씩 높아지며, 최소 두 자리이고 재사용하지 않는다.
        보존 스트림은 `log.md`의 항목 바이트, 그다음 번호 내림차순의 보관 파일 항목 바이트를
        연결하며 각 파일 안에서는 최신순이다. 줄바꿈 정규화 없이 분할 전후 바이트가 같아야 한다.
        생성한 보관 머리말과 마커로 표시한 보관 색인 표는 메타데이터이므로 그 스트림에서 뺀다.
      - 분할 보관과 복구를 포함한 모든 위키 명령은 하나의 배타적 위키 잠금을 잡는다. 잠금 아래에서
        로그와 보관 집합을 스냅샷으로 남기고, 보관 파일과 교체 로그를 준비한 뒤, 경로·원본과 교체
        바이트/다이제스트·단계를 담은 저널을 상태 폴더에 영속적으로 쓴다.
        게시 직전에 원본 바이트와 보관 집합을 다시 검증한다. 다르면 어느 파일도 게시하기 전에
        `OAH_WIKI_CONCURRENT_EDIT`로 거절한다.
      - 기존 경로를 덮어쓰지 않고 보관 파일을 먼저 게시한 뒤 로그(생성한 보관 색인 포함)를
        원자적으로 교체한다. 단계마다 파일과 디렉터리를 flush하고 둘 다 영속적으로 저장된 뒤에만
        저널을 제거한다. 개별 쓰기는 원자적 교체를 쓴다.
      - 저널을 제거하기 전에 두 파일을 다시 읽는다. 둘 다 정확히 게시한 바이트이면 저널을
        제거한다. 아니면 모든 위키 파일을 그대로 두고 현재 바이트를
        `changed-after-publication` 사유와 함께 저널에 기록한다. 명령은
        `OAH_WIKI_RECOVERY_PENDING`으로 거절한다. 이 사유에서는 `oah wiki recover`도
        `OAH_WIKI_RECOVERY_CONFLICT`로 거절하고 다른 파일과 저널을 명시한다(A18).
      - 위키를 읽거나 쓰기 전에 잠금 아래에서 미완료 저널을 확인한다. `oah wiki recover` 외의
        명령은 `OAH_WIKI_RECOVERY_PENDING`으로 거절하고 위키 파일과 저널을 바꾸지 않으며
        사용자에게 `oah wiki recover`를 명시적으로 실행하라고 안내한다. 복구 명령은 같은 잠금
        아래에서 실행한다. 원래 로그가 바뀌지 않았고 새 보관 파일이 없거나 일치하면 앞으로
        진행해 완료한다. 교체 로그와 보관 파일이 모두 일치하면 이미 완료한 상태다.
        복구는 멱등적이다. 그 밖의 바이트는 `OAH_WIKI_RECOVERY_CONFLICT`로 거절하고
        모든 파일과 저널을 보존하며 다른 파일과 저널을 명시한다. 잠금 프로토콜 밖의 편집기는
        게시 중에 쓰기를 멈춰야 한다. 게시 전이나 복구 중에 발견한 편집은 위와 같이 거절한다.
      - 모든 `OAH_WIKI_RECOVERY_CONFLICT`에서 사용자는 파일을 수동으로 조정한 뒤
        같은 위키 잠금 아래에서 `oah wiki recover --resolved`를 실행한다. 로그와 모든
        보관 파일을 파싱할 수 있고 중복 항목이 없는지 검사한다. 하나라도 실패하면
        `OAH_WIKI_RECOVERY_CONFLICT`로 거절하고 아무것도 바꾸지 않는다. 모두 통과하면
        저널의 원래 스냅샷이나 수집한 현재 바이트에는 있지만 조정한 파일에는 없는 모든 항목을
        정확한 항목 바이트 비교로 먼저 나열한다. 누락 항목은 해결 처리를 막지 않는다.
        이어 저널을 바이트 그대로 상태 폴더 아래 `resolved-records/`의 타임스탬프가 있는
        해결 기록으로 옮기고, 해결 시각과 기록 경로를 이벤트에 기록한다.
        `--resolved`는 저널 바이트를 삭제하지 않는다(A18).
    - **`oah wiki hygiene`**는 일반 린트와 색인 린트의 정의에 따라 `broken_links`, `unindexed`,
      `missing_sources`, `missing_frontmatter`, `orphans`, `duplicate_titles`, `stale`, `banners`,
      `overlong_lines`의 `count(delta)` 값을 한 줄로 출력한다. 스캔 성공 뒤 개수와 실제 린트
      설정을 상태 폴더에 원자적으로 저장한다. 첫 실행이나 설정 변경 때는 모든 계수에
      `delta=n/a`를 출력하고 기준값을 만든다. 그 밖에는 현재 개수에서 이전 성공 실행의 개수를
      뺀 부호 있는 차이를 표시한다. 스캔이 실패하면 기준값을 바꾸지 않는다.
    - **선택형 커밋 검사 `oah hook pre-commit`**은 다음과 같은 커밋을 차단한다.
      - 보관 파일을 추가하지 않고 `log.md`를 줄이는 커밋.
      - 기존 보관 파일을 편집하거나 삭제하는 커밋.
      - `raw/` 아래 파일을 편집하거나 삭제하는 커밋.
      - 위키 파일에 UTF-8이 아닌 바이트나 서로게이트 바이트를 추가하는 커밋.

      새로 깨진 링크를 추가하는 커밋에는 먼저 경고하며,
      `[wiki] new_broken_links = "block"`을 설정하면 차단한다.
    - **`oah init`**은 그래프 확대 수준 같은 편집기 UI 상태를 git에서 무시하도록 한다.
      예를 들면 `.obsidian/workspace*.json`이다.
- **스킬 유지 관리(A15).** 자동 검사는 작업 트리 밖의 상태 폴더에 있는 키트 캐시만 갱신한다.
  추적하는 프로젝트 스킬 폴더는 핀이다. 바꾸려면 위키 유지 관리 예외 없이 자체 범위와
  리스를 가진 별도 작업에서 `oah skills update --project`를 명시적으로 실행해야 한다.
  선택 우선순위는 프로젝트 사본, 키트 캐시, 사용자·플러그인 사본 순이다.
- **그래프(선택).** `oah graph update|query|path|explain|report`는 graphify에 얇게 전달하는
  명령이며 graphify가 설치돼 있을 때 실행한다(`uv tool install graphifyy`). `oah init --with-graph`는
  `.graphifyignore`를 쓴다(위키 로그, 빌드 출력, `.oah/tasks/*/GATES.md` 증거).
- **영향 지도.** `oah impact <path>… [--changed-since REV] [--strong-only] [--json] [--ref REV]`는
  결정적 계층만 쓴다. 결과는 결속 순서(pin > exec > link > mention)로 정렬한다.
  지도는 워크트리별로 상태 폴더에 저장한다. 최신 여부는 워크트리 신원과 실제 입력 명세에 결속한다.
  HEAD SHA, 추적 인덱스 상태 다이제스트, 지도가 읽는 변경·미추적 입력의 경로/바이트 다이제스트,
  모든 원장 바이트(무시된 원장 포함), 설정 바이트, 도구 버전이 명세에 들어간다. 신원이나 명세가
  바뀌면 지도는 오래된 상태가 된다. 결과 다이제스트도 기록한다. 빌드 전후 명세를 비교하며
  입력이 바뀌면 재시도하거나 오래된 상태로 거절해야 한다.
  **최신 기본 브랜치(A23).** 작업은 한 레인씩 기본 브랜치에 반영되므로 최신 기본 브랜치를 기준으로
  영향을 평가한다. 5.9의 반영 계약은 반영 직전에 다시 fetch하고 영향을 재평가하며 새로 영향받는
  검사를 요구한다. 리뷰한 정확한 head의 fast-forward push에서 원격이 평가한 SHA를 강제한다.
  질의 전에 `oah impact`는 사용자의 git 설정으로 원격의 기본 브랜치를 읽기 전용으로 fetch하고
  상태 폴더에 그 ref의 두 번째 지도를 갱신한다. 출력은 두 지도 모두 커밋 SHA로 표시한다.
  `[impact] refresh_default_branch = true`가 기본값이다. `--offline`이거나 fetch가 실패하면
  마지막으로 가져온 ref를 쓰고 `default-branch=stale(<reason>)`으로 표시한다. 알리지 않고 쓰지 않는다.
  1. **게이트 결속.** 원장을 게이트별로 파싱한다. 경로를 해시·검색·고정하는 CHECK나 EXPECT는
     **pin**, 실행하는 것은 **exec**다. 출력은 `.oah/tasks/T/GATES.md#G3 binding=exec`처럼
     게이트를 명시한다. 파일에서 게이트를 수동으로 찾아야 했던 원본의 빈틈을 메운다.
  2. **어휘 참조.** ref에서 경로, 기본 파일명, 확장자를 뺀 이름을 `git grep -n -I -F`로 찾는다.
     결과는 glob에 따라 워크플로, 테스트, 문서, 설정, 매니페스트, 마이그레이션으로 분류한다.
  3. **위키 참조.** 파일을 언급하는 위키 페이지이며 업데이트할 지식이다.
  4. **동시 변경.** 해당 경로를 건드린 최근 N개 커밋에서 함께 바뀐 파일이다.
  5. **그래프 이웃.** graphify 출력이 있으면 읽고 없으면 알림 없이 건너뛴다.
- **`--changed-since REV`**는 변경 파일을 재실행할 게이트 목록으로 바꾼다. 바뀐 만큼만 검증하는
  규칙을 명령으로 만든 것이다. `--strong-only`는 모든 pin·exec 결과를 상한 없이 나열하고
  `total/shown/overflow`로 끝낸다. 종료 코드는 0 정상, 2 사용법, 69 환경이다.
- **재정렬기(선택형 플러그인, 나중에).**
  - 설정한 재정렬기는 영향 결과의 순서를 바꿀 수 있다. 결정적 pin·exec 행은 제거하지 않는다.
  - 제공자는 사용자가 명시적으로 켰고 키도 있을 때만 실행한다. 안내형 `oah init`이 한 번 묻고
    발생할 수 있는 비용과 전송할 후보/증거 데이터를 설명하며 선택을 기록한다. 사용자가 대신
    설정에서 켤 수도 있다. 키만 있다고 켜지지 않는다. 브리프에서도 끌 수 있다
    (`--no-rerank` 또는 패킷에 명시).
  - 모든 영향 출력과 영향을 사용한 모든 보고는 `rerank=used` 또는
    `rerank=unused(<reason>)`을 밝힌다. 사유에는 `not-enabled`, `no key`, `disabled-by-brief`,
    `error:<code>`가 있다.
- **DB 백엔드 플러그인(나중에, 규칙은 이미 적용).**
  - DB 객체 지도 같은 프로젝트별 백엔드는 `db_table:<name>`, `db_routine:<name>`,
    `db_grant:<name>` 같은 개체에 JSON을 출력하는 명령으로 연결한다.
  - DB 테이블, 루틴, GRANT를 건드리는 모든 작업과 리뷰는 그 플러그인을 질의해야 한다.
  - 보고에는 질의와 그래프 식별 정보인 커밋 SHA·출력 다이제스트가 있어야 한다.
  - 플러그인이 없는 프로젝트는 조용히 건너뛰지 않고 `db-impact: unavailable (no plugin configured)`를 반환한다.

### 5.11 규칙 블록과 tier 에이전트

- `oah rules sync|check`는 `AGENTS.md`의 `<!-- OAH:RULES:BEGIN v1 -->`과
  `<!-- OAH:RULES:END -->` 사이 블록 하나를 관리한다.
  - 파일이 없으면 만들고, 블록이 없으면 추가하며, 교체할 때는 블록 내용만 바꾼다.
  - 마커가 중복되거나 깨졌으면 아무것도 쓰지 않고 거절한다. 줄바꿈을 유지하며 원자적으로 쓴다.
  - 쓴 뒤 `AGENTS.md`가 Codex 프로젝트 문서 제한인 32 KiB 미만인지 확인한다.
  - `CLAUDE.md`에는 Claude Code가 지원하는 `@AGENTS.md` 가져오기 줄 하나를 넣는다.
    두 엔진이 중복 없이 같은 텍스트를 읽는다.
  - 블록 템플릿은 영어와 한국어(`--lang`)로 제공한다. 프로젝트별 규칙으로 `.oah/rules.local.md`를 덧붙인다.
  - 규칙·tier 동기화는 전역 파일을 쓰지 않는다. 자동 스킬 업데이트는 상태 폴더의 키트 캐시만 쓴다.
    추적하는 프로젝트 스킬 핀을 바꾸려면 자체 범위를 가진 별도 리스 작업에서
    `oah skills update --project`가 필요하다. 전역 스킬 업데이트는 확인을 받는
    `oah skills update --global`이 필요하다(A15).
- **블록 자체는 6 KB 미만이다.** 단계 순서, 리스, 패킷과 의도, 사전 점검, 게이트와 바뀐 만큼만
  검증하는 규칙, 리뷰 루프와 정직한 표시, "워커는 push하지 않는다", 위키 최신 상태 유지,
  "배정받은 워커(`OAH_WORKER=1`)는 모든 세션 진입 절차를 건너뛰고 프롬프트의 작업만 한다"를
  담는다. A15의 캐시 전용 자동 업데이트, 프로젝트 스킬 핀, 별도 리스의 프로젝트 업데이트,
  5.8의 unlazy 핀 검증도 적는다.
- `oah init --with-agents`는 `.claude/agents/oah-{low,medium,high,max}.md`를 쓴다.
  frontmatter에는 `name`, `description`과 설정에서 가져온 `model`, `effort`를 둔다.
  본문에는 범위 준수, 증거 인용, 파괴적 git 금지, 막혔을 때 정직하게 보고하라는 규칙을 적는다.
- `oah tiers sync|check`는 frontmatter의 `model`, `effort` 줄과 본문의 관리 규칙 블록
  (`<!-- OAH:TIER-RULES:BEGIN v1 -->` … `<!-- OAH:TIER-RULES:END -->`)을 관리한다.
  - 동기화는 매번 정본 텍스트로 블록을 **교체**하므로 표현 변경이 기존 파일에도 전달된다.
  - 블록에는 규칙과 공유 의도 푸터를 담는다(A7).
  - 블록이 없으면 삽입하고 마커가 깨졌으면 거절한다.
  - `check`는 frontmatter나 블록이 정본과 다르면 차이를 보고한다.

### 5.12 훅 — 모두 선택형이며 프로젝트에만 설치하고 제거할 수 있다

| 훅 | 명령 | 단계 |
|---|---|---|
| Claude Code Stop. 관할 리스의 게이트가 미충족이면 차단 | `oah hook stop` | P5 |
| Claude Code SessionStart. 활성 리스, 최근 로그 항목, 최신 인계문. `OAH_WORKER=1`이면 출력 없음 | `oah hook session-start` | P7(11행) |
| git pre-push. 이 워크트리에 끝나지 않은 리스가 있으면 거절. gitleaks가 설치돼 있으면 push할 커밋 검사 | `oah hook pre-push` | P5(3행, A17) |
| Claude Code PreToolUse. 게이트, 워크플로, 마이그레이션 편집 전 영향 힌트 | `oah hook impact-hint` | P7(A11) |
| git pre-commit. 위키 커밋 검사(보관 파일, 원본 자료, 인코딩, 새로 깨진 링크) | `oah hook pre-commit` | P7 |
| git post-commit과 post-merge. 영향 지도를 증분 갱신 | `oah hook post-commit`, `oah hook post-merge` | P5 설치 프로그램, P7 명령(A11) |

`oah hooks install|uninstall|status`는 쓰기 전에 정확한 변경을 보여 준다. 원자적으로 쓰고 백업을 남긴다.
`oah hook <name>`은 `oah.hook_<name>` 모듈을 실행한다. P5가 설치 프로그램을 담당하고 각 단계는
설치 프로그램을 편집하지 않고 자기 훅 모듈을 추가한다. `oah init`은 소유자의 기본값으로 모든 훅을
제안하며 사용자가 하나씩 확인한다(6.5).

### 5.13 v1 명령 모음

| 그룹 | 명령 |
|---|---|
| 설정 | `doctor`, `init`(안내형, 6.5), `models`(A14), `skills list/install/update`(A13, A15) |
| 작업 | `packet new/check`, `intent show/check`, `preflight`, `start`, `renew`, `status`, `finish`, `release`, `log`, `handoff`(11행) |
| 엔진 | `recommend`, `call` |
| 게이트 | `gates new/status/check/lint`, `check exists/contains/absent/json` |
| 리뷰 | `review`, `fix`, `review summary`, `land --check/--merge`(29행) |
| 지식 | `wiki new/log/maintain/lint/rollover/recover/hygiene`(`lint --index` 포함), `graph …`, `impact`, `impact build/refresh`(A11) |
| 에이전트 파일 | `rules sync/check`, `tiers sync/check` |
| 훅 | `hooks install/uninstall/status`, `hook stop/pre-commit/pre-push/post-commit/post-merge/session-start/impact-hint` |

`oah doctor`는 git, Python, Node(게이트를 쓸 때만), uv, claude, codex(`codex login status`)와
한 카탈로그의 권장·선택 도구를 검사한다(A13). 빠진 도구마다 이 OS의 공식 설치 명령을 출력하고,
필수 도구가 없으면 0이 아닌 코드로 종료한다.

---

## 6. OS별 설치

### 6.1 사전 요구 사항(공식 출처, 2026-10-23 확인)

| 도구 | 필요한 용도 | macOS | Windows | 공식 페이지 |
|---|---|---|---|---|
| Git | 모든 작업 | `xcode-select --install` 또는 `brew install git` | Git for Windows 설치 프로그램 또는 `winget install --id Git.Git -e --source winget` | https://git-scm.com/install/mac · https://git-scm.com/install/windows |
| uv(Python 3.11+도 설치함) | `oah`와 graphify 설치 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | https://docs.astral.sh/uv/getting-started/installation/ |
| Python 3.11+(uv를 쓰지 않을 때만) | `oah` 런타임 | python.org 설치 프로그램 또는 Homebrew | Python Install Manager(python.org) 또는 `winget install Python.Python.3.13` | https://www.python.org/downloads/ · https://docs.python.org/3/using/windows.html |
| Node.js LTS(현재 24, 26은 2026-10-28에 LTS가 됨) | unlazy 게이트 검사기(Node 16+) | `brew install node@24` 또는 nodejs.org 설치 프로그램 | `winget install OpenJS.NodeJS.LTS` | https://nodejs.org/en/download |
| Claude Code | 1차 구현 단계 | `curl -fsSL https://claude.ai/install.sh \| bash` | PowerShell: `irm https://claude.ai/install.ps1 \| iex`(WinGet: `Anthropic.ClaudeCode`) | https://code.claude.com/docs/en/setup |
| Codex CLI | 리뷰와 수정 단계 | `brew install --cask codex` 또는 `npm install -g @openai/codex` | `powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 \| iex"` | https://github.com/openai/codex · https://developers.openai.com/codex/windows/windows-sandbox |
| unlazy(게이트에 필수) | 게이트 검사 실행, Stop 훅 | `oah init --with-unlazy`(P5 핀, 커밋과 스크립트 다이제스트 검증) | 동일 | https://github.com/Leonxlnx/unlazy |
| graphify(선택) | 지식 그래프 | `uv tool install graphifyy` | 동일 | https://docs.graphify.com/installation |
| GitHub CLI(선택) | 터미널에서 PR 작업 | `brew install gh` | `winget install --id GitHub.cli --source winget` | https://cli.github.com |
| gitleaks(선택) | push 전 비밀값 스캔 | `brew install gitleaks` | `scoop install gitleaks` | https://github.com/gitleaks/gitleaks |
| Aside(선택) | 브라우저와 웹 작업 | 다운로드 페이지의 설치 프로그램 | 다운로드 페이지의 설치 프로그램 | https://aside.com/download · CLI/MCP: https://docs.aside.com/help/developers |
| Orca(선택, v1에서 사용하지 않음) | 다중 에이전트 데스크톱 앱 | `brew install --cask stablyai/orca/orca` 또는 다운로드 페이지 | 다운로드 페이지의 `.exe` 설치 프로그램 | https://www.onorca.dev/docs/install |

**계정.**
- Claude Code에는 Pro, Max, Team, Enterprise, Console 계정이 필요하다. 무료 플랜에는 포함되지 않는다.
- Codex는 ChatGPT 플랜 로그인(Plus, Pro, Business, Edu, Enterprise)이나 API 키를 받는다.

**시스템.**
- Claude Code에는 macOS 13+ 또는 Windows 10 1809+가 필요하다.
- Codex는 Windows 11을 권장하며 Windows 10은 최선 노력으로 지원한다.
- Claude Code에서 Git for Windows는 선택 사항이다. 없으면 PowerShell을 쓴다.

### 6.2 macOS — `install/install.sh`

스크립트는 bash 3.2와 호환된다(macOS에 3.2가 들어 있다). 클론에서 `bash install/install.sh`로 실행한다.

1. **탐지.** git, uv 또는 Python ≥ 3.11, Node, claude, codex, gh, gitleaks, graphify를
   찾고 상태 표를 출력한다.
2. **해결 방법 표시.** 빠진 도구마다 6.1의 공식 명령을 출력한다. `--yes`가 있거나 도구별
   y/N 질문에 동의하면 명령을 실행한다. Homebrew가 있으면 쓰고 없으면 공식 한 줄 명령을 쓴다.
   동의 없이는 아무것도 바꾸지 않는다.
3. **`oah` 설치.** `uv tool install --force <clone>`을 쓰고, 대안으로 pipx, 그다음
   PATH 안내를 붙인 `pip --user`를 쓴다.
4. **PATH 확인.** 새 셸에서 `oah`를 찾을 수 있는지 확인한다. 못 찾으면
   `uv tool update-shell`을 제안한다. 스크립트는 알리지 않고 셸 프로필을 편집하지 않는다.
5. **선택 추가 항목.** `--with-unlazy`, `--with-graphify`다.
6. **검증.** `oah doctor`를 실행한다.

`--dry-run`은 실행하지 않고 모든 단계를 출력한다. CI가 이 옵션을 쓴다.

### 6.3 Windows — `install/install.ps1`

**실행 방법.** 클론에서 PowerShell을 열고
`powershell -ExecutionPolicy Bypass -File .\install\install.ps1`을 실행한다. 정책은 그 프로세스 하나에만
적용하며 시스템 설정은 바꾸지 않는다. 또는 `Unblock-File .\install\install.ps1`을 쓰면 검토한 파일을
`RemoteSigned` 아래에서 실행할 수 있다.

**스크립트 호환성 규칙.**
- Windows PowerShell 5.1과 PowerShell 7에서 실행한다.
- 스크립트 텍스트는 **ASCII만** 쓴다. PowerShell 5.1은 BOM 없는 UTF-8을 잘못 읽으므로
  한국어 도움말은 문서에 둔다.
- 저장소의 `.gitattributes`는 `.ps1`에 CRLF, `.sh`에 LF를 적용한다.

**단계.** macOS와 같되 다음 차이가 있다.
- `Get-Command`를 쓴다.
- git, Node, gh, Python에는 `winget`을, uv, Claude Code, Codex에는 공식 PowerShell 한 줄 명령을 쓴다.
- 설치할 때마다 레지스트리에서 `$env:Path`를 갱신해 새 도구를 같은 창에서 쓸 수 있게 한다.

**인코딩.**
- `oah`는 모든 파일과 실행 로그를 UTF-8로 쓴다. 하위 프로세스 stdout/stderr를 바이트로 받으며,
  명시적 인코딩(기본 UTF-8, 도구별 cp949 설정 가능)과 `errors="replace"`로 디코딩한다.
- 출력 전에 리디렉션된 스트림을 포함해 `oah`의 stdout과 stderr를 기존 인코딩과
  `errors="backslashreplace"`로 설정한다. `reconfigure`가 없는 스트림에는 동등한 인코딩 안전
  작성기를 쓴다. 인코딩할 수 없는 이모지와 U+FFFD는 이스케이프하며 치명적 오류로 만들지 않는다.
- 네이티브 Windows CI는 공백이나 한국어 사용자 이름이 있는 경로와, 한국어·이모지·잘못된
  하위 프로세스 바이트를 담은 리디렉션 cp949 stdout/stderr를 다룬다. `UnicodeEncodeError`가 없어야 한다.

### 6.4 WSL 전용 대신 네이티브 Windows를 쓰는 이유

- Claude Code는 네이티브 Windows 설치 프로그램을 제공하고 PowerShell로 셸 명령을 실행한다.
- Codex는 네이티브 샌드박스가 있는 네이티브 Windows 설치 프로그램을 제공한다. WSL은 대안으로 문서화돼 있다.
- `oah`는 표준 라이브러리 Python이며 네이티브로 실행한다.
- WSL을 필수로 하면 초보자에게 이득 없이 Linux 배포판, 두 번째 파일 시스템, 두 번째
  자격 증명 묶음을 추가하게 된다. WSL 사용자도 macOS/Linux 경로를 쓸 수 있다.

### 6.5 초보자 온보딩(개정 3)

초보자가 따라가는 전체 경로이며 매 단계에서 쉬운 한국어나 영어를 쓴다. 유료이거나
되돌릴 수 없는 일은 사용자가 명시적으로 확인해야만 일어난다.

1. **OS별 명령 하나를 두 배포 방식으로 제공한다.**
   - **비공개 저장소.** README 명령은 클론에서 실행한다. macOS는 `bash install/install.sh`,
     Windows는 `powershell -ExecutionPolicy Bypass -File .\install\install.ps1`이다.
     먼저 git을 설치해야 한다고 문서에 밝힌다. macOS는 `xcode-select --install`, Windows는
     `winget install --id Git.Git -e`를 쓴 뒤 저장소를 클론한다.
   - **공개 저장소.** README는 macOS에 `curl <pinned-release-bootstrap-url> | bash`,
     Windows에 `irm <pinned-release-bootstrap-url> | iex`를 제시한다. 부트스트랩은 git 없이
     고정 릴리스 압축 파일을 내려받아 압축을 풀기 전에 릴리스의 기대 다이제스트와 SHA-256을
     대조한다. uv와 `oah`를 설치하고 동의를 받아 git 설치를 제안한다. 소유자가 공개할 때까지
     릴리스 URL과 다이제스트는 자리표시자다. 비공개 방식은 미리 써 보는 경로이며 공개 방식이
     AC1의 새 컴퓨터 경로를 제공한다.
   - 두 방식 모두 설치와 전역 변경에 확인을 받는다(AC6).
   - 새 셸에서 `oah`를 찾을 수 있는지 확인한다.
   - `oah init`으로 넘긴다.
2. **`oah init`은 안내형 설정이다.**
   - **도구.** 카탈로그(`oah.setup`)의 모든 도구를 검사하고 빠진 도구마다 공식 설치 명령과
     URL을 보여 준다. 사용자가 확인한 도구만 설치한다. `--yes`만 전체 동의를 뜻하며
     그 밖의 것은 동의로 보지 않는다.
   - **로그인.** Claude Code와 Codex 로그인을 안내한다. 사용자가 자기 터미널에서 실행할
     명령을 알려 주고 해당 도구 자체 상태 명령으로 결과를 검증한다. `oah`는 비밀번호, 키,
     토큰을 읽거나 저장하거나 요구하지 않는다.
   - **환경.** 소유자의 작업 흐름과 같은 기본값으로 프로젝트 환경을 만든다.
     - Claude → Codex → Codex 단계
     - 업데이트를 따라가는 난이도별 모델의 tier 에이전트(A10, A14)
     - 규칙 블록(5.11)
     - Obsidian 위키(A12)
     - 첫 영향 지도(A11)
     - 게이트 템플릿과, 동의한 경우 P5 계약 테스트 커밋의 unlazy. 모든 게이트 명령 전에
       커밋과 스크립트 다이제스트를 검증한다(5.8).
     - 자동 스킬 선택과 기본 일일 확인을 하는 키트 캐시 업데이트. 추적하는 프로젝트 스킬은
       자체 범위·리스를 가진 작업에서 `oah skills update --project`를 명시하기 전까지 고정한다(A9, A15).
     - 편집기 상태와 로컬 비밀값 파일을 위한 `.gitignore` 항목(A8d, A17)
     - 먼저 보여 주고 동의할 때만 쓰는 프로젝트용 기본 GitHub Actions 워크플로(A21)
     - 추가하는 기능을 설명하며 제안하는 선택형 동반 앱. Orca는 워크트리에서 에이전트를 나란히
       실행하고 Aside는 MCP를 통한 웹 에이전트 역할을 한다(A20).
     - 사용자가 수락한 각 선택형 훅(5.12)
   - 두 번째 실행은 오래된 영향 지도를 갱신하고 그 밖에는 바꾸지 않는다(7절).
3. **첫 안내형 작업.** `examples/hello-oah`는 패킷, 사전 점검, 리스, Claude 구현, 게이트,
   Codex 리뷰와 수정, 재리뷰, 완료, 선택형 PR까지 전체 과정을 끝까지 안내한다.
4. **쉬운 말.** 문서와 메시지는 영어와 한국어로 제공하며(P9), 모든 오류에 다음에 실행할 명령을 적는다.

작업은 P8(설치 프로그램과 도구 카탈로그), P8c(동반 앱, A20), P7c(CI 스타터, A21),
P7b(`oah init`), P9(예제와 문서) 레인으로 나눈다.

## 7. `oah init`

개정 3은 `oah init`을 6.5의 안내형 설정으로 만든다. 도구 검사, 동의받은 설치,
로그인 안내를 실행한 뒤 아래의 환경과 저장할 첫 영향 지도를 만든다. 각 `--with-*` 옵션은
안내 흐름의 질문이 되고, 플래그를 주면 질문 없이 답한 것으로 처리한다.


```text
oah init [--lang en|ko] [--with-agents] [--with-hooks] [--with-unlazy] [--with-graph] [--dry-run] [--force]
```

- **쓰기 전에 계획한다.** 만들거나 바꿀 파일 목록을 출력한 뒤 없는 파일만 쓴다.
  `--force` 없이 기존 파일을 덮어쓰지 않는다. 관리 블록은 예외이며 `rules sync`가 그 자리에서 갱신한다.
- **항상 만드는 것.** `.oah/config.toml`(5.5의 기본값과 탐지한 기본 브랜치), `.oah/templates/`,
  `AGENTS.md`의 관리 블록, `CLAUDE.md`의 `@AGENTS.md` 줄,
  `docs/wiki/`(색인, 마커가 있는 로그, 결정 템플릿)다.
- **영향 지도.** 뼈대를 만든 뒤 init은 A11의 실제 입력 명세로 워크트리별 첫 지도를 만들어 저장한다.
  빌드가 실패하면 경고와 `impact=unavailable`을 기록하고 나머지 init은 실패시키지 않는다.
  빌드 재시도 명령을 출력하며, 사용할 수 없는 지도를 최신으로 표시하지 않는다.
- **모의 실행.** `--dry-run`은 예정된 설치·파일 변경과 지도 빌드를 나열하고 어느 것도 실행하지 않는다.
- **옵션.**
  - `--with-agents`: tier 에이전트 파일.
  - `--with-graph`: `.graphifyignore`와 `oah graph update` 실행 안내.
  - `--with-unlazy`: 확인을 받은 뒤 P5 계약 테스트를 거친 unlazy 커밋을 프로젝트 사본으로 설치하고
    커밋·스크립트 다이제스트를 검증한다(5.8). `--force`는 호환되지 않는 사본을 명시적으로 교체한다.
    버전 없는 설치 명령은 쓰지 않는다.
  - `--with-hooks`: diff를 보여 준 뒤 Stop 훅 설치.
- **스킬 업데이트.** 안내형 설정은 기본적으로 `update_interval_days = 1`인 캐시 전용 자동
  업데이트를 켠다(A15). 추적하는 프로젝트 스킬은 핀으로 유지하며 자체 범위·리스를 가진 작업의
  `oah skills update --project`로만 바꾼다. 전역 업데이트에는 확인을 받는 `--global`이 필요하다.
- **멱등성.** 두 번째 실행은 같은 실패 처리 방식으로 오래되거나 사용할 수 없는 지도를 갱신한다.
  지도가 최신이고 입력이 같으면 아무것도 바꾸지 않고 그 사실을 알린다.

---

## 8. 테스트와 CI

**단위 테스트**는 표준 라이브러리 `unittest`를 써서 테스트 의존성이 없다. 대상은 다음과 같다.
- 동시 프로세스의 잠금(`multiprocessing`, 두 플랫폼 분기 모두)
- 원자적 쓰기
- 리스 겹침, TTL, `finish` 가드
- 설정 기본값과 검증
- 라우팅 점수
- 엔진 명령줄. 네이티브 Windows CI에서 공백과 Windows에서 유효한 메타 문자(`& ^ % ! ( )`)가
  들어간 경로, `& | < > ^ % ! ( )`가 들어간 설정 인자로 npm shim 해석과 미지원 배치 shim
  거절을 검사한다. argv가 문자 그대로 전달되고 주입된 명령이 실행되지 않아야 한다.
- 패킷과 의도 파싱. CRLF와 LF 파일의 다이제스트가 같은지도 포함한다.
- 스텁 `node` 스크립트를 쓰는 게이트 상태 파서와 unlazy 위임. CHECK가 그대로여도 소스가
  바뀌면 `--reverify`가 실행돼야 한다. 재실행 실패는 finish를 막아야 하고, 입력 동일 승계는
  통과해야 하며, 검사 도중 입력이 바뀌면 실패해야 한다.
- 모든 자유 텍스트 필드에 경로와 개인 식별자 픽스처를 넣는 요약 내보내기. 검증한 ID,
  고정 상태, 다이제스트만 남아야 한다.
- 6.3의 리디렉션 cp949 사례를 포함한 하위 프로세스 디코딩과 인코딩 안전 stdout/stderr
- 임시 저장소의 영향 결속
- 규칙 블록 동기화. 멱등성, 손상 마커 거절, 줄바꿈 보존, 32 KiB 제한
- `init`의 멱등성, 덮어쓰기 방지, 모의 실행
- **가짜 codex**로 검사하는 리뷰. 스냅샷 제거, 카나리, 스키마 검증, 수렴, 판단 중단, 선택형 라운드 예산.
  토큰 파일만 읽는 가짜 리뷰어도 카나리 검증을 통과할 수 있다. 이 통과를 소스 검토 범위나
  트리 신원 검증으로 표시해서는 안 된다.
- 임시 bare 원격 저장소에서의 사전 점검 판정
- Stop 훅 판단 JSON
- 설치 프로그램 모의 실행

**가짜 엔진**은 설정이 가리키는 작은 Python 스크립트다
(`command = ["python", "tests/fakes/fake_codex.py"]`). `command`가 목록인 이유 가운데 하나다.

**개발 Mac의 로컬 실행**은 `TMPDIR=$PWD/.tmp python3.11 -m unittest discover -s tests -v`를 써서
모든 테스트 쓰기가 이 체크아웃 안에 머무르게 한다(`.tmp/`는 git에서 무시한다).
로컬의 최신 Python으로도 다시 실행한다.

**CI**는 `.github/workflows/ci.yml`이다.
- `main` 대상 `pull_request`와 `workflow_dispatch`에서 실행하고 문서만 바뀌면 건너뛴다
  (`docs/**`, 루트 `*.md`).
- `macos-latest`와 `windows-latest`에서 Python 3.11로 각각 작업 하나를 실행하며 더 큰 매트릭스를 두지 않는다.
- `concurrency`로 대체된 이전 실행을 취소한다. `permissions: contents: read`다. 액션은 전체
  커밋 SHA로 고정한다. 제한 시간은 15분이다.
- 단계는 단위 테스트, 설치 프로그램 `--dry-run`(macOS의 bash, Windows의 `powershell` 5.1과
  `pwsh` **모두**), 임시 프로젝트의 `oah init --dry-run`이다.
- 별도 워크플로 `secrets.yml`은 문서만 바뀐 경우를 포함해 모든 pull request와 push에서 실행한다
  (A17). ubuntu-latest 작업 하나에서 SHA-256으로 고정한 gitleaks 8.30.1이 전체 이력을
  스캔하고, `tools/privacy_scan.py --gitleaks require`가 트리와 커밋 신원을 스캔한다.
  하나라도 발견하면 실패한다.

**비용.**
- 비공개 저장소의 정가는 macOS 분당 $0.062, Windows 분당 $0.010, Linux 분당 $0.006이다.
  GitHub Pro에는 월 3,000분의 표준 러너 사용 시간이 포함된다
  (https://docs.github.com/en/billing/reference/actions-runner-pricing).
- macOS 약 3분과 Windows 4분으로 예상하는 실행 한 번은 포함 시간을 다 쓴 뒤 정가로 약 $0.23이다.
- 포함 시간을 macOS와 Windows 러너에 어떻게 차감하는지는 검증하지 않았다.
- 공개 저장소의 표준 러너는 무료다.

**이 Mac에는 PowerShell도 Windows도 없다.** 따라서 Windows 동작은 **CI로만** 검증한다.
소유자는 2026-10-23에 P1부터의 CI를 승인했다.

## 9. 라이선스와 외부 구성 요소

| 구성 요소 | 라이선스 | 키트의 사용 방식 |
|---|---|---|
| 이 키트 | **MIT**, "Copyright (c) 2026 open-agent-harness contributors" | — |
| unlazy(Leonxlnx/unlazy) | MIT | `oah init`이 P5 계약 테스트 핀으로 설치하는 운영 의존성. 커밋·스크립트 다이제스트 검증(5.8). 번들 대신 연결한다. 업스트림 저장소 파일 하나가 그대로는 이 저장소 개인정보 스캔을 통과하지 못하기 때문이다(A13) |
| graphify(Graphify-Labs/graphify, PyPI `graphifyy`) | Apache-2.0(NOTICE에 따르면 이전 기여는 MIT) | CLI로 실행하는 선택 의존성. 번들 대신 연결한다. 업스트림에는 스킬 폴더가 없고 CLI가 템플릿에서 스킬을 쓰기 때문이다(A13) |
| Claude Code | Anthropic 약관 | 사용자가 설치하는 외부 도구 |
| Codex CLI(openai/codex) | Apache-2.0 | 사용자가 설치하는 외부 도구 |
| uv | Astral(이중 MIT / Apache-2.0, 미검증) | 사용자가 설치하는 외부 도구 |
| superpowers(obra/superpowers) | MIT | 선택형 권장 항목. 번들 대신 연결한다. 두 파일이 그대로는 이 저장소 개인정보 스캔을 통과하지 못하기 때문이다(A13) |
| mattpocock 스킬(mattpocock/skills) | MIT | 고정 커밋의 원본을 `skills/mattpocock-skills/` 아래 그대로 번들하고 LICENSE와 `THIRD_PARTY_NOTICES.md` 항목을 함께 둔다(A13) |
| netwaif/multi-agent-starter | MIT | 아이디어만 사용(파일을 기억으로 사용, 백엔드 레지스트리, 재진입), 출처 표시 |
| netwaif/knot | **라이선스 없음** | 아이디어만 사용하고 출처 표시. 텍스트나 코드는 복사하지 않음 |

`THIRD_PARTY_NOTICES.md`에 이를 나열하고, "Vendored skills" 표에 `skills/` 아래 모든 폴더를 적는다.
무엇이든 원문 그대로 복사할 때는 라이선스 텍스트를 먼저 넣는다.

**개정 3(A13).** 검증한 업스트림 라이선스가 허락할 때만 스킬을 번들한다. 15절에 모든 항목과
판정을 나열한다. 번들 스킬은 `skills/<name>/` 아래 원본 그대로 두고 LICENSE, 업스트림 URL,
고정한 커밋을 함께 제공한다. `THIRD_PARTY_NOTICES.md` 항목은 같은 PR에서 추가한다.

## 10. 이 저장소의 개인정보 보호 장치

`tools/privacy_scan.py`(표준 라이브러리)를 **매** push 전에 실행한다.

- git 이력 전체에 `gitleaks detect`를 실행하며 발견한 비밀값은 가린다.
- 추적·스테이징 파일에서 정규식으로 다음을 찾는다.
  - macOS와 Windows의 개인 절대 홈 경로(자리표시자는 허용)
  - 소유자 식별자와 정확히 일치하는 단어
  - 제품 이름
  - 이메일 주소(GitHub noreply 허용)
  - API 키·접근 토큰 형태(OpenAI, GitHub classic·fine-grained, Slack)
  - 대문자 환경 변수 형식의 키·토큰·비밀값 할당
- push할 커밋의 작성자와 커미터 이메일을 검사한다.
- `PRIVACY_SCAN_CLEAN` 또는 발견 항목을 출력한다.
- 허용 목록의 항목마다 한 줄 이유를 붙인다.

개발 컴퓨터에는 훅을 설치하지 않는다. 스캔은 절차의 수동 단계이며 모든 보고에 결과를 포함한다.
모든 pull request와 push의 CI, 릴리스 태그 전 전체 이력에서도 실행한다. 하나라도 발견하면
빌드를 실패시키며, 실제 자격 증명처럼 보이는 값은 어떤 허용 목록 항목으로도 제외하지 않는다(A17).

---

## 11. 구현 단계

각 단계는 비공개 저장소의 `main`으로 보내는 브랜치 하나와 PR 하나다.

| 단계 | 내용 | 리뷰 추론 강도 |
|---|---|---|
| P0 | 이 계획 | medium |
| P1 | 뼈대. LICENSE, 고지, `pyproject.toml`, `oah --version`, `oah doctor`, `.gitattributes`, `.gitignore`, `tools/privacy_scan.py`, 테스트 뼈대, CI 워크플로(승인됨) | medium |
| P2 | 상태와 리스. 경로, 잠금, 원자적 쓰기, 리스 저장소, `start/renew/status/finish/release/log` | highest = `max`(동시성, Windows 잠금) |
| P3 | 설정, 라우팅, 엔진. 자동 적용하는 `recommend`(A10), 어댑터, 단계를 강제하는 `call`, 실행 디렉터리, 프로세스 트리 종료, 스킬 선택과 재정렬기 인터페이스(A9), 모델 카탈로그(A14), 키 조회(A17), 선택형 Orca 어댑터 | high |
| P4 | 패킷, 의도, 사전 점검. `packet new`의 스킬과 난이도(A9, A10), 영향 갱신(A11) | high |
| P5 | 게이트. 파서, unlazy 위임, `check` 도우미, `finish` 가드, Stop 훅과 `hooks install`, git 훅(pre-push, post-commit, post-merge) | high |
| P6 | 리뷰 루프. 스냅샷, 카나리, 스키마, 영수증, 라운드, `fix`, `review summary`, 영향 갱신(A11), `land --check`(29행) | highest = `max`(신뢰 경계) |
| P7 | 지식과 에이전트 파일. 안내형 `init`(6.5), `rules`, `tiers`, Obsidian `wiki`(A12), `graph`, SQL 플러그인이 있는 영속 `impact` 지도(A11), 툴킷과 스킬 업데이트(A13, A15), `handoff`와 SessionStart(11행) | high |
| P8 | 설치 프로그램. `install.sh`, `install.ps1`, 모의 실행, 설치 안내, 도구 카탈로그와 안내형 설치(6.5), `doctor` 도구 검사(A13) | highest = `max`(원격 설치 프로그램 실행) |
| P9 | 문서. 영어/한국어 README, 개념, 따라 하기 예제(`examples/hello-oah`), 문제 해결, 툴킷 페이지, 업그레이드 경로(A16), 사용자 키 보관 위치(A17) | medium |

**레인(개정 3).** 빨리 끝내도록 단계를 병렬 레인으로 만든다. 각 레인은 브랜치 하나,
워크트리 하나, Claude 구현자 하나, Codex 리뷰 시리즈 하나이며 의존 순서에 따라 PR 하나로
반영한다(레인 표는 15절). 필요한 것이 반영된 뒤에만 그 레인을 `main` 위로 rebase한다.

**PR별 절차**는 소유자가 정한 엔진 순서를 따른다.

1. **Claude가 구현한다.** 가능한 곳은 테스트부터 쓴다. Python 3.11과 로컬 최신 Python에서
   테스트를 실행한 뒤 개인정보 스캔을 한다.
2. **Codex가 리뷰한다(R1).** 브랜치 head의 정제한 스냅샷에서 카나리와 출력 스키마를 써서
   `codex exec --ephemeral -s read-only`를 실행한다. 모델은 소유자가 고른 리뷰어 모델
   (현재 `gpt-6.1-sol`)이다. 추론 강도는 위 표를 따른다. "highest"는 모델이 지원한다고
   나열한 가장 높은 강도(5.5)이므로 가장 강한 설정은 `max`다. 실행기는 시작 전에 모델과
   메타데이터를 해석하고 헤더가 보고한 설정을 `launch_effort`와 비교하며 정규화한
   `resolved_inference_effort`는 따로 기록한다(5.5). 불일치면 라운드는 실패한다.
   헤더를 읽을 수 없거나 정규화 규칙을 모르면 강도는 미검증이다. 둘 다 강도 검증 리뷰로
   세지 않는다. P6 이후에는 자체 도구인 `oah review`로 이 절차를 실행한다.
3. **Codex가 지적을 고친다.** 이 저장소 워크트리에서 `codex exec -s workspace-write`를 쓴다.
   Claude가 결과를 Codex 수정으로 표시해 커밋한다. 이어 Codex가 재리뷰한다.
   - **상시 수정 라운드 규칙**(소유자, 2026-10-23, 모든 단계). 모든 새 지적이 P0 또는 P1이고,
     작고, 단계 범위 안이며, 이전 라운드에서 수정을 시도한 적 없는 유형이면 자동으로
     다음 수정과 재리뷰를 계속한다.
   - 이전 라운드에서 수정을 시도한 유형이 반복되거나, 설계·저장 형식 변경이 필요하거나,
     다음 라운드가 R9 이상일 때만 소유자에게 묻는다.
   - P2 또는 P3 지적만 남으면 체크리스트를 실행하고 병합하며 지적은 후속 작업으로 기록한다.
4. **push하고 PR을 연다.** 개인정보 스캔이 깨끗한 뒤 진행한다. CI는 PR에서 실행한다.
   Claude는 Codex 리뷰가 수렴하고 스캔이 깨끗하며 CI가 통과하면 병합한 뒤 링크를 보고한다.
5. **멈출 때마다 보고한다.** 만든 것, 커밋·PR 링크, 테스트 명령과 결과, 스캔 결과, 열린 질문을 담는다.

**작업 규칙.**
- 이 저장소는 원본 하네스의 잠금 CLI를 사용하지 않는다.
- P2가 반영되면 `oah` 자체 리스를 쓴다. 상태는 허용된 쓰기 영역 안에 있는
  이 저장소 자체 git 공통 디렉터리에 둔다.
- 이 저장소 자체 개발용 게이트 원장은 증거에 로컬 경로가 들어갈 수 있으므로 추적하지 않는다.
  - 게이트는 별도의 개발 워크트리에서 실행하며 `--root` / `--cwd`를 주면 그곳을 가리키게 한다.
    `UNLAZY_APPROVAL_DIR`은 주 체크아웃의 `.git/oah-dev/unlazy-approved`를 가리킨다.
    이는 정규 경로로 해석한 뒤에도 해당 워크트리 루트 밖이면서 이 저장소 체크아웃 안에 쓰기를
    유지하는 위치다. 주 체크아웃 자체에서 이 게이트를 실행하지 않는다. 승인 저장소가 그 루트
    안에 있게 되어 unlazy가 그 저장소를 거절하기 때문이다.
  - 구현자의 2026-10-23 unlazy 2.1.0 시험은 그 승인 저장소를 사용해 개발 워크트리의
    시험용 원장에 `gate-check.mjs --approve --cwd .`를 실행했다. 승인 파일이 쓰였고
    게이트는 `ALL MET (1 met)`, 종료 코드 0을 반환했다. P5는 고정한 unlazy 커밋에서 이 시험을
    반복하고 주 체크아웃에서 실행하면 거절되는지 테스트한다.
  - 키트 사용자는 unlazy 기본 승인 저장소를 유지한다.

## 12. 위험

| 위험 | 대응 |
|---|---|
| 개발 Mac에서 Windows를 테스트할 수 없음 | P1부터 CI(승인 필요), Windows 전용 테스트(경로, CRLF, cp949, `.cmd` shim) |
| Claude Code / Codex CLI 플래그가 자주 바뀜 | `engines/`에 어댑터 격리, 가짜 엔진 테스트, `oah doctor` 버전 출력, 문서에 테스트 버전 나열 |
| unlazy 2.1.0은 미출시 소스 대상임 | 문서에서 커밋 고정, 대안으로 내장 구조 상태 검사 |
| graphify 출력 형식 변경 | 그래프는 형식 변화에 관대한 파싱을 하는 선택 계층 |
| Codex `workspace-write`가 `.git`을 읽기 전용으로 만듦 | 상위 프로세스의 `oah`가 상태 쓰기, `OAH_STATE_DIR`로 위치 재정의 |
| Claude Code의 선택형 샌드박스가 안에서 띄운 엔진을 막을 수 있음(Codex가 다른 샌드박스 안에서 자체 샌드박스 시작, 아직 미검증) | P3에서 검증, 문서에서 `oah`를 Claude 샌드박스 밖에서 실행하도록 안내 |
| 에이전트 셸 도구가 약 10분 뒤 명령을 멈춤 | `--background`와 `oah wait`(5.0) |
| Windows에서 백신이나 색인기가 파일을 잠금 | 대기 간격을 늘리는 `os.replace` 재시도, 짧은 잠금 구간 |
| 초보자의 PATH 문제 | 설치 프로그램이 새 프로세스에서 `oah`를 확인하고 정확한 해결 명령 출력 |
| 범위 확대(원본에는 하위 명령 33개가 있음) | 5.13의 v1 명령 모음, 나머지는 모두 "나중에"로 표시 |

## 13. 결정

**소유자 결정**(2026-10-23 답변).

1. **라이선스.** MIT, 저작권자 줄은 "open-agent-harness contributors"다.
2. **커밋 신원.** 이 저장소는 소유자의 GitHub noreply 주소로 커밋한다. PR이 생기기 전에
   커밋 하나뿐이던 `main`을 그 주소로 다시 쓰고 한 번 강제 push했다.
3. **CI.** 워크플로는 P1에서 반영하며 PR마다 macOS 작업 하나와 Windows 작업 하나를 실행한다.
4. **병합.** Claude는 Codex 리뷰가 수렴하고 개인정보 스캔이 깨끗하며 CI가 통과하면
   각 PR을 병합하고 링크를 보고한다.
5. **이 계획.** 소유자가 2026-10-23에 승인했다. P1을 시작해도 된다.
6. **의도 개정 2**(2026-10-23). 1절의 초보자 우선 제품 구상이다. AC3과 AC7의 추가 사항과 함께 승인했다.
7. **개정 3**(2026-10-23). 소유자 메모에서 가져온 A9–A17과 온보딩 흐름이다(15절).
8. **수정 라운드.** 11절의 상시 규칙을 모든 단계에 적용한다.
9. **병렬 레인.** 독립된 단계는 각각 Claude 세션과 Codex 리뷰를 두고 나란히 진행하며
   의존 순서에 따라 반영한다.
10. **릴리스.** v0.1.0 체크리스트로 구현을 마친다(15절). 저장소를 공개하거나 사람을
    초대하는 일은 소유자가 결정한다.
11. **자격 증명(A17).** 모든 단계와 PR의 엄격한 규칙이다.

**구현자의 결정**은 어느 것이든 바꿀 수 있다.

*런타임과 배치*
- Python 표준 라이브러리 CLI.
- 기본 설치 프로그램은 uv.
- 명령 이름은 `oah`, 배포 이름은 `open-agent-harness`.
- 상태는 git 공통 디렉터리에 저장.
- 작업별 폴더 `.oah/tasks/<task>/`.
- 위키는 `docs/wiki/`.
- Orca는 선택형 어댑터로만 쓴다(8행).

*작업 흐름*
- 사용자의 대화형 Claude Code 세션이 리드이며 1차 구현을 한다.
- 긴 실행은 `--background`와 `oah wait`를 쓴다.
- 헤드리스 Claude 기본값은 `acceptEdits`이며 `bypassPermissions`가 아니다.
- 리스 범위는 작업 중 지침이며 finish, review, fix에서 검사한다.
- GitHub는 선택 사항이며 사전 점검은 원격 저장소가 없으면 WARN으로 처리한다.

*게이트와 리뷰*
- 게이트에는 unlazy와 그에 따른 Node가 필수다. 구조 확인만으로 끝내려면 명시적 플래그가 필요하다.
- `GATES.md`는 git에서 무시하고 경로 없는 요약을 커밋한다.
- 리드가 게이트를 실행한다.
- 작성 엔진은 커밋 트레일러에서 읽으며 `unknown` 표시도 쓴다.
- 리스를 인식하고 unlazy 훅으로 넘기는 Stop 훅.
- `oah check` 도우미.
- 리뷰 영수증은 상태에 보관하고 PR에는 `review summary` 사용.

*에이전트 파일*
- `AGENTS.md`에 규칙 블록 하나를 두고 `CLAUDE.md`가 가져온다.

*문서와 테스트*
- 모든 초보자 문서는 영어와 한국어로 제공한다.
- 테스트 임시 파일은 체크아웃 안에 둔다.

*개정 3*
- A13 판정은 검증한 업스트림 라이선스를 따른다(15절). MCP 서버에서 "template"은
  `${ENV_VAR}` 자리표시자가 있는 실행 템플릿을 뜻한다. 서버 자체는 실행할 때 레지스트리에서 설치한다.
- 비밀값 스캔은 macOS·Windows 테스트 작업과 별도의 Linux 작업 하나로 실행한다.
- 독립된 부분을 먼저 반영할 수 있도록 P3, P4, P7을 레인으로 나눈다(15절).

## 14. 이 계획의 수치 근거

모든 명령은 원본 루트에서 읽기 전용으로 실행한다.

| 수치 | 명령 |
|---|---|
| 파일 286개, 7.7 MB | `find . -path ./.git -prune -o -type f -print \| wc -l` · `du -sh .` |
| 10,635줄, 하위 명령 33개 | `wc -l bin/mah` · `grep -oE 'add_parser\(\s*"[a-z0-9_-]+"' bin/mah \| sort -u \| wc -l` |
| 보조 모듈 39개(Orca 12개, OpenCode 11개) | `ls bin/mah_*.py \| wc -l` · `ls bin/mah_orchestration*.py \| wc -l` · `ls bin/mah_opencode_*.py \| wc -l` |
| 계약 테스트 69개 | `find test -maxdepth 1 -type f ! -name '*.pyc' \| wc -l` |
| 규칙 문서 ~180 KB, 규칙 조각 ~130 KB | `cat AGENTS.md TASK_ROUTING_POLICY.md ORCA_ADVERSARIAL_REVIEW.md README.md \| wc -c`(179,293) · `cat shared/*.md docs/*/*.md \| wc -c`(129,627) |
| 홈 경로 / 제품 이름 / 키체인 언급이 있는 파일 57 / 47 / 23개 | `grep -rlI --exclude-dir=__pycache__ --exclude-dir=.git '<pattern>' . \| wc -l` |
| 픽스처 하나의 홈 경로 포함 줄 681개 | `grep -c '<home path>' test/fixtures/<fixture>.jsonl` |
| 단계 표(원본의 라운드 상한은 가져오지 않음, A22) | `grep -nE '^REVIEW_ROUND_CEILING' bin/mah` · `config/routing.json` 5–9줄 |

## 15. 개정 1–5(2026-10-23)

소유자가 2026-10-23에 자기 하네스를 고친 내용을 소유자 요청으로 다섯 묶음에 걸쳐
이 계획에 반영했다. A1–A6, A7–A8, 개정 3(A9–A17과 온보딩 흐름), 개정 4(A18–A19),
개정 5(A20–A21)다. 각 항목에 바꾼 절과 체크리스트에 반영할 단계를 명시한다.

| # | 규칙 | 절 | 단계 |
|---|---|---|---|
| A1 | 추론 강도는 모델 지원값에서 정하며 고정 상한이 없다. `highest`는 지원하는 가장 높은 추론 강도이며 `gpt-6.1-sol`에서는 `max`다. 실행 전에 모델을 정해야 하고 `highest`에는 쓸 수 있는 모델 메타데이터가 필요하다. 헤더는 실행 설정을 검증하고 버전별 정규화는 예상 추론 강도를 따로 기록한다. 강도가 낮아지면 실행을 실패시키고 미검증 강도는 정직하게 표시한다. 강도를 알리지 않고 낮추지 않는다. | 5.5, 11 | P2 리뷰, P3, P6, 이 저장소 실행기 |
| A2 | "네트워크 금지"는 `oah impact`나 동의받아 켠 재정렬기를 끄지 않는다. 제공자에는 명시적 활성화와 키가 필요하며(A9), 브리프에서 끌 수 있다. 모든 결과는 `used` 또는 `unused(reason)`을 보고하며 `unused(not-enabled)`, `unused(no key)`도 포함한다. | 4(17행), 5.6, 5.10 | P4, P7 |
| A3 | DB 테이블, 루틴, GRANT를 건드리는 작업이나 리뷰는 DB 백엔드 플러그인을 질의하고 질의와 그래프 신원(커밋 SHA, 다이제스트)을 반환한다. 플러그인이 없으면 없다고 밝힌다. | 5.6, 5.10 | P4, P6, P7 |
| A4 | 빈 `## Scope`는 뚜렷이 경고한다(`OAH_SCOPE_EMPTY`). Scope의 파일 종류에 의존하는 선택은 조용히 빈 결과를 선택하지 않는다. | 5.6 | P4, P7 |
| A5 | `oah tiers sync`는 관리 규칙 블록이 없을 때뿐 아니라 매번 정본 텍스트로 교체한다. | 5.11 | P7 |
| A6 | 배정받은 워커에는 `OAH_WORKER=1`을 넣는다. 생성한 훅과 규칙 블록은 모든 세션 진입 절차를 건너뛰라고 지시한다. | 5.5, 5.11, 5.12 | P3, P7(레인 P7b의 session-start 훅) |
| A7 | 의도 계보. 모든 단계, 워커, 서브에이전트에 하나의 의도 절을 쓴다. 목표가 바뀌면 새 ID 대신 개정을 올린다. 조건은 결과다. 리스는 같은 start 명령에서 절에 결속하고 상태는 `current`가 된다. 보고에는 `INTENT`와 AC별 줄을 담는다. 공유 푸터 하나를 모든 브리프와 tier 에이전트 파일에 넣는다. 초안은 결속할 수 없다. | 5.6, 5.11 | P3, P4, P6, P7 |
| A8 | 위키 자동 정리는 후보만 제시한다. 린트는 실제 문제만 센다. 크기 기반 로그 분할 보관, 선택형 커밋 검사, 편집기 UI 상태 추적 제외, 위키 밖에 쓰는 색인 린트, 정리 상태 변화량 한 줄을 제공한다. | 5.1, 5.10, 5.12 | P7 |

### 단계별 체크리스트

**P2**
- [ ] 지원하는 가장 높은 추론 강도(`max`)로 리뷰한다. 실행기의 헤더 설정 검사와 추론 정규화를 검증한다(5.5). (A1)

**P3**
P3b는 호출 어댑터와 브리프 구성을 담당한다. P4b는 아래 호출 거절과 공유 푸터 통합을 포함해
`start`, `status`, `finish`, `call`의 모든 의도 연결을 담당한다.

- [ ] Codex tier 강도는 `supported_reasoning_levels`에서 정한다. `highest`는 동작을 바꾸는 모드를 건너뛴다. 쓸 수 있는 메타데이터가 있는데 직접 적은 강도가 목록에 없으면 `OAH_EFFORT_UNSUPPORTED`로 실패한다. (A1)
- [ ] tier 모델 핀이 먼저이고, 다음은 A14의 자동 카탈로그 매핑, 다음은 실제 Codex 설정(선택한 프로필 포함)이다. 구체적인 모델을 `-m`으로 전달하고 기록한다. 새 설치에서 기본 모델을 정할 수 없으면 `OAH_MODEL_UNRESOLVED`로 거절하고 tier 모델을 설정하라고 안내한다. (A1, A14)
- [ ] 캐시가 없거나 모델 항목이 없거나 쓸 수 있는 일반 추론 단계가 없을 때 `highest`는 `OAH_EFFORT_METADATA_MISSING`과 실행 가능한 안내로 거절한다. 메타데이터가 없고 강도를 직접 적었으면 그대로 전달한다. (A1)
- [ ] 가짜 엔진 테스트는 일치, 더 낮음, 그 밖의 불일치, 읽을 수 없는 헤더를 다룬다. 낮으면 `OAH_EFFORT_DOWNGRADED`, 그 밖의 불일치는 `OAH_EFFORT_MISMATCH`, 읽을 수 없으면 `unverified`다. 헤더는 설정만 검증한다. (A1)
- [ ] 버전/모델별 정규화 테스트에 `ultra` 같은 동작 변경 별칭과 알 수 없는 매핑을 포함한다. 실행 기록은 요청, 실행, 헤더 보고, 해석된 추론 강도를 따로 보관하고 헤더 검사 상태와 정규화 근거도 둔다. 알 수 없는 추론 강도는 `unverified`로 남긴다. (A1)
- [ ] 워커는 `OAH_WORKER=1`, `OAH_RUN_ID`, `OAH_TASK`와 함께 띄우고 사전 안내문의 첫 줄에 워커임을 적는다. (A6)
- [ ] 작업 의도가 `current`가 아니면 `oah call`이 배정을 거절한다(`OAH_INTENT_NOT_CURRENT`). 모든 워커 브리프에 공유 의도 푸터를 붙인다. (A7)

**P4**
P4a는 패킷, 의도, 사전 점검 핵심을 담당한다. P4b는 `start`, `status`, `finish`, `call`의
모든 의도 연결을 담당한다. P6는 P4b 반영 뒤 그 통합을 쓴다.

- [ ] `oah packet check`가 `OAH_SCOPE_EMPTY`로 경고하고, `oah start --packet`은 `--scope repo`가 없으면 빈 Scope를 거절한다. (A4)
- [ ] 패킷 템플릿은 "네트워크 금지"가 무엇을 금지하고 무엇은 금지하지 않는지 밝힌다. (A2)
- [ ] Return 절에 `rerank=used|unused(<reason>)`과 DB 영향 줄을 담는다. (A2, A3)
- [ ] `oah packet new`는 초안 의도를 만든다. `oah start --packet`은 같은 명령에서 `{source, id, revision, digest}`에 결속하고 초안을 `OAH_INTENT_NOT_ACCEPTED`로 거절한다. 리스 기록에 선택형 `intent` 필드가 추가되므로 스키마는 버전 1을 유지한다. (A7)
- [ ] `oah status`와 `oah intent show`는 5.6의 조건을 순서대로 적용한다. 결속·원본 부재, 승인되고 일치하는 신원/다이제스트, 상태/신원/다이제스트 변경, 읽을 수 없는 원본, 누락·손상·중복 마커를 테스트한다. 잘못된 내용은 절대 `current`가 아니다. (A7)
- [ ] 목표가 바뀌면 같은 ID의 `Revision`을 올린다. 같은 작업의 다른 `Intent-ID`는 `OAH_INTENT_ID_CHANGED`로 거절한다. (A7)
- [ ] 성공 조건이 절차 단계처럼 읽히면 `oah packet check`가 `OAH_INTENT_PROCESS_STEP`으로 경고한다. (A7)
- [ ] `oah finish`는 5.6처럼 보고의 `INTENT`, `AC<n>` 줄 구조를 결속과 대조한다. 잘못된 보고는 `OAH_INTENT_RETURN_INVALID`로 거절한다. (A7)

**P6**
- [ ] 헤더 설정이나 정규화한 추론 강도가 낮은 리뷰 라운드는 `OAH_EFFORT_DOWNGRADED`로 실패한다. 그 밖의 헤더 불일치는 `OAH_EFFORT_MISMATCH`로 실패한다. 실패한 라운드는 세지 않으며, 읽을 수 없는 헤더나 알 수 없는 정규화는 강도 검증 리뷰로 세지 않는다. (A1)
- [ ] DB 변경의 리뷰 프롬프트는 DB 영향 질의와 그래프 신원을 요구한다. 리뷰어는 둘이 있는지 검사한다. (A3)
- [ ] 리뷰어는 배정받은 워커로 실행한다. (A6)
- [ ] 리뷰와 수정 브리프는 같은 의도 절을 바이트 그대로 담고, 라운드 지시는 그 밖에 두며 공유 푸터를 붙인다. 영수증에 다이제스트를 기록한다. (A7)
- [ ] `oah review`와 `oah fix`는 current가 아닌 모든 상태에서 배정 전에 `OAH_INTENT_NOT_CURRENT`로 거절한다. 두 명령 모두 잘못된 원본 내용을 포함해 `unbound`, `missing`, `stale`을 테스트하고 `current`는 허용한다. (A7)
- [ ] `oah review`는 보고의 `INTENT` 신원/개정/다이제스트 구조를 결속과 대조하고 결속한 AC마다 정확히 하나의 유효한 상태/증거 줄을 요구한다. 테스트는 누락·손상·중복·불일치 `INTENT` 줄과 누락·손상·중복·지어낸 AC 줄을 `OAH_INTENT_RETURN_INVALID`로 거절하며 완전한 보고는 받아들인다. `oah finish`도 같은 검사를 쓴다. (A7)

**P7**
- [ ] `oah impact`는 항상 `rerank=...`을 출력한다. 제공자는 명시적으로 켰고 키가 있을 때만 실행하며 브리프에서 끌 수 있다. 키가 있지만 켜지 않았을 때 `unused(not-enabled)`, 켰지만 키가 없을 때 `unused(no key)`를 테스트한다. (A2, A9)
- [ ] DB 백엔드 플러그인 계약을 마련하고 DB 개체에 질의와 그래프 신원을 요구한다. (A3)
- [ ] Scope 파일 종류에 따른 모든 선택은 빈 Scope를 경고하고 조용히 빈 결과를 선택하지 않는다. (A4)
- [ ] `oah tiers sync`가 관리 블록을 교체한다. 테스트에서 정본 표현을 바꾸면 기존 파일에 반영되는지 확인한다. (A5)
- [ ] 규칙 블록이 배정받은 워커에게 세션 진입을 건너뛰라고 지시한다. (A6)
- [ ] tier 에이전트 파일의 관리 블록 안에 공유 의도 푸터가 있으며 동기화할 때마다 교체한다. (A5, A7)
- [ ] `oah wiki lint`는 실제 문제만 센다. 위키 밖 링크 루트를 해석하고 코드 구간·블록의 링크를 무시하며 보관 색인을 색인 출처로 세고 마지막 커밋 날짜로 오래됐는지 판단한다. (A8a)
- [ ] `oah wiki rollover`는 5.10의 정확한 기준값, 전체 항목 파싱, 초과 항목 규칙, 보관 번호, 메타데이터 없는 연결 순서를 구현한다. 기준값 경계, 잘못된 경계, 펜스 안 제목, 여러 보관 파일, 초과 항목을 테스트하며 보존 스트림을 바이트 단위로 비교한다. (A8b)
- [ ] 분할 보관은 위키 접근을 직렬화하고 준비한 쓰기를 저널에 기록·flush하며 보관 파일부터 게시하기 전에 원본을 검증한다. 미완료 저널이 있으면 `oah wiki recover` 외 명령은 `OAH_WIKI_RECOVERY_PENDING`으로 거절하고 5.10처럼 명시적 복구를 안내한다. 읽기 전용 린트가 거절할 때 위키 파일과 저널 바이트를 바꾸지 않는지 테스트한다. 모든 저널/게시 단계의 실패 주입과 반복적인 명시적 복구로 항목 손실·중복이 없음을 증명한다. 동시 편집 테스트는 게시 전 거절과 복구 충돌 시 보존을 증명한다. (A8b)
- [ ] `oah hook pre-commit`은 선택형이다. 보관 파일 누락, 보관 편집, 원본 자료 편집, 비 UTF-8·서로게이트 바이트를 차단한다. 새로 깨진 링크는 먼저 경고하고 이후 차단한다. (A8c)
- [ ] `oah init`은 편집기 UI 상태를 git에서 무시하게 한다. (A8d)
- [ ] `oah wiki lint --index`는 5.10의 배너 패턴과 설정 가능한 120 코드 포인트 줄 제한을 쓰며, 일반 린트 규칙으로 미색인 노트를 찾고 위키 밖에 후보를 쓴다. 패턴 일치, 펜스 코드 제외, 줄 제한 경계를 테스트한다. (A8e)
- [ ] `oah wiki hygiene`는 5.10의 이름 있는 계수 아홉 개를 출력하고 개수/설정을 원자적으로 저장한다. 첫 실행과 설정 변경의 `delta=n/a`, 이후 부호 있는 변화량, 실패한 스캔의 기준값 보존을 테스트한다. (A8f)
- [ ] 어떤 위키 명령도 스스로 노트를 삭제·병합·이동하지 않는다. 후보만 만든다. (A8)
- [ ] `oah hook session-start`는 `OAH_WORKER=1`이면 아무것도 출력하지 않는다. P7b 레인에서
  안내형 `oah init`의 훅 연결과 함께 만든다. (A6)

### 소유자의 후속 수정에서 나온 후보(2026-10-23)

개정 2 이후 얻은 교훈이다. 각각은 **후보**이며 해당 단계를 계획할 때 체크리스트 항목이 된다.
제외할 때는 이유를 기록해야 한다.

| # | 후보 | 단계 |
|---|---|---|
| C1 | **2026-10-24에 P4b 레인(PR #31)으로 반영한 소유자 결정으로 대체했다.** `oah call`로 배정한 워커는 배정자의 기존 리스를 쓰며 시작·갱신·해제·완료하지 않는다. 모든 브리프에는 리스 명령 대신 리스 범위와 의도 결속을 담는다. 쓰기 워커(1차 구현·수정 단계)는 배정자가 리스를 잡은 워크트리에서만 실행한다. Orca 실행기도 그 워크트리를 재사용하며, `--worktree`가 다른 트리를 가리키면 `OAH_WORKTREE_LEASE_MISMATCH`로 거절한다. 읽기 전용 리뷰는 어느 트리에서든 실행할 수 있다. 리스 저장소가 이를 강제한다. `oah call`이 모든 실행기에 설정하는 `OAH_WORKER=1`이면 모든 리스 쓰기를 `OAH_WORKER_LEASE_REFUSED`로 거절한다. 재사용 명세가 여전히 리스 명령을 언급하면 `OAH_WORKER_LEASE_COMMAND` 경고만 받는다. 테스트는 배정자의 `oah start --packet`을 실행하고 `oah intent show`가 `current`를 보고하는지 확인한다. *대체된 이전 규칙은 다음과 같다.* 구현·수정 브리프에만 정확한 리스 명령(`oah start --packet <source>`)을 담고 리뷰 브리프에는 넣지 않으며 테스트는 전달된 명령을 따른다. | P3, P4, P6 |
| C2 | 브리프 배치는 고정한다. 워커 줄, 사전 안내문, 의도 절 원문, 라운드 지시, 검증 푸터(바뀐 만큼만 검증, 5.11), 의도 푸터 순이다. 이전 기준본과 비교하는 테스트는 그 위치에서 의도 푸터를 정확히 하나 제거하며 푸터가 없거나 중복이면 실패한다. | P3, P6 |
| C3 | 저장한 브리프나 명세를 재사용하는 배정도 정본 의도 푸터를 담은 전달용 브리프를 구성한다. 전달할 수 없으면 `OAH_FOOTER_NOT_APPENDED`로 배정을 거절한다. | P3 |
| C4 | 푸터 시작 마커가 줄 시작에서 줄 전체를 차지할 때만 푸터가 있는 것으로 센다. 마커를 언급만 하는 브리프에도 푸터를 붙인다. tier 동기화는 관리 블록에 같은 규칙을 쓴다. | P3, P7 |
| C5 | 현재 문서는 "max is rejected" 같은 고정 추론 강도 상한을 주장하지 않는다. P9에서 쓰는 문서와 이후 편집은 지원 강도 규칙(A1)을 따른다. | P9 |
| C6 | 앞으로의 스킬 선택 문구는 실제 선택(종류, 단계, 위험, 가용성)을 설명하고 선택형 스킬을 하나도 쓰지 않는다고 약속하지 않는다. | 나중에 |

**후보 체크리스트 줄**은 각 단계를 계획할 때 그 단계 체크리스트에 복사한다.
- **P3.**
  - [ ] 어떤 브리프에도 리스 명령을 넣지 않는다. 모든 브리프에 배정자의 리스 범위와 의도 결속을 담는다. (C1, 2026-10-24 대체)
  - [ ] 브리프는 고정 배치를 쓴다. (C2)
  - [ ] 저장한 브리프나 명세를 재사용할 때 정본 의도 푸터를 담은 브리프를 전달한다. 못 하면 `OAH_FOOTER_NOT_APPENDED`로 배정을 거절한다. (C3)
  - [ ] 푸터가 있다는 것은 줄 시작의 마커 줄이 있다는 뜻이다. (C4)
- **P4.**
  - [ ] 테스트는 배정자의 `oah start --packet`을 실행하고 `oah intent show`에서 `current`를 얻는다. `OAH_WORKER=1`이면 모든 리스 쓰기를 `OAH_WORKER_LEASE_REFUSED`로 거절하고 쓰기 워커는 리스를 잡은 워크트리에서만 실행한다. (C1, 2026-10-24 대체)
- **P6.**
  - [ ] 리뷰·수정 워커는 배정자의 리스를 쓰며 리스 명령을 받지 않는다. 수정은 리스를 잡은 워크트리에서 실행하고 리뷰는 어느 트리든 쓸 수 있다. (C1, 2026-10-24 대체)
  - [ ] 기준본 비교는 고정 위치에서 의도 푸터를 정확히 하나 제거하며, 없거나 중복이면 실패한다. (C2)
- **P7.**
  - [ ] tier 동기화는 줄 시작의 마커 줄로 관리 블록을 찾는다. (C4)
- **P9.**
  - [ ] 어떤 문서도 고정 추론 강도 상한을 주장하지 않는다. (C5)
- **나중에.**
  - [ ] 스킬 선택 문구는 종류, 단계, 위험, 가용성에 따른 실제 선택을 설명하고 선택형 스킬을 하나도 쓰지 않는다고 약속하지 않는다. (C6)

### 개정 3(2026-10-23)

소유자의 2026-10-23 메모에서 가져온 항목이다. 각각 바꾸는 절과 담당 단계를 명시한다.
1절(의도 개정 3), 4절(재판정한 행), 6.5(온보딩), 11절(레인과 수정 라운드 규칙),
13절(결정)을 이 내용으로 갱신했다.

| # | 규칙 | 절 | 단계 |
|---|---|---|---|
| A9 | **스킬 자동화.** `oah`는 설치된 Claude·Codex 스킬 디렉터리(사용자, 프로젝트, 플러그인 스킬)와 키트 캐시에서 A15 우선순위로 브리프마다 스킬을 고른다. 작업 종류, 단계, 위험, Scope의 파일 종류에 따라 선택하고 이유와 함께 브리프에 참조하거나 첨부한다. Scope가 비면 경고한다(A4). 재정렬기 플러그인 인터페이스는 후보 순서를 바꿀 수 있으며 첫 제공자는 TypeSafe Jev다. 제공자는 명시적으로 켰고 키도 있을 때만 실행하며 키만으로는 켜지지 않는다. 안내형 `oah init`이 한 번 묻고 발생할 수 있는 비용과 전송할 후보/증거 데이터를 알리며 선택을 기록한다. 또는 사용자가 설정에서 켠다. 모든 결과는 `used` 또는 `unused(<reason>)`을 기록하며 `unused(not-enabled)`, `unused(no key)`도 포함한다. 선택 사용 기록은 로컬에 둔다. 선택기는 새 코드이며 원본의 스킬 장치를 복사하지 않는다. | 4(30, 31행), 5.5, 5.6, 5.10 | P3, P4, P7 |
| A10 | **난이도별 모델·추론 강도를 자동 적용한다.** `oah packet new`와 `oah call`은 난이도 축(kind, intensity, reasoning, risk)을 규칙으로 채점하고 수동 플래그 없이 `oah recommend`를 적용한다. `--tier`로 재정의할 수 있고 기록한다. 재정렬기 플러그인은 참고용 AI 판정을 더할 수 있으며 규칙 결과 옆에 기록하고 tier를 낮추지는 않는다. 실행 기록에는 축, tier 출처, 요청한 모델·강도와 실제 모델·강도를 보관한다. | 4(6, 31행), 5.5, 5.6 | P3, P4 |
| A11 | **영향 지도를 생성하고 최신으로 유지한다.** `oah init`이 상태 폴더에 워크트리별 지도를 만들어 저장한다. 실패하면 경고와 `impact=unavailable`을 기록하고 나머지 init은 실패시키지 않는다(7). 선택형 `post-commit`, `post-merge` 훅으로 증분 갱신하며 `preflight`, `packet new`, `review`, `finish`는 먼저 지도를 갱신한다. 최신 여부는 워크트리 신원과 실제 입력 명세에 결속한다. HEAD SHA, 추적 인덱스 상태 다이제스트, 지도가 읽은 변경·미추적 입력 다이제스트, 무시된 원장을 포함한 원장 바이트, 설정 바이트, 도구 버전이다. 무엇이든 바뀌면 오래된 상태가 되며 결과 다이제스트도 기록한다. 오래된 지도는 갱신하거나 `OAH_IMPACT_STALE`로 거절하며 알리지 않고 쓰지 않는다. 기본 DB 플러그인은 SQL 마이그레이션에서 테이블, 뷰, 루틴, 트리거, 인덱스, GRANT를 추출해 코드 참조와 연결하므로 SQL 프로젝트는 맞춤 코드 없이 DB 영향 그래프를 얻는다. 재정렬기는 선택 사항으로 유지한다. 편집 시점 영향 힌트는 선택형 훅이 된다. | 4(16–18행), 5.10, 5.12, 7 | P4, P5, P6, P7 |
| A12 | **Obsidian 위키를 자동 생성·관리한다.** `oah init`은 Obsidian 호환 `docs/wiki/`를 만든다. 색인, 삽입 마커가 있는 최신순 로그, 노트 템플릿, workspace 파일을 git에서 무시하는 최소 `.obsidian/` 설정이다. 리드나 안내형 기본 절차는 수정 뒤를 포함해 최종 커밋, 게이트, 리뷰 전에 추적 로그·색인·분할 보관을 위해 `oah wiki maintain --task T`를 실행한다. 5.4의 출력은 범위 예외이며 이벤트에 기록한다. 유지 관리는 위키 잠금, 제한된 재시도, 작업별 멱등 삽입, 명시적 저널 복구를 쓴다(5.10). `finish`는 추적 파일을 쓰지 않는다. 작업 유지 관리를 검증하며, 기록되는 `--allow-missing-wiki-maintenance` 재정의 없이는 거절한다. `finish`와 pre-commit의 린트·정리 상태 검사는 추적 파일을 바꾸지 않는다. graphify는 선택 사항(`oah graph`)이고 그래프를 자동 재구축하는 동작은 없다. | 4(21행), 5.1, 5.4, 5.10, 5.12 | P7 |
| A13 | **출처가 있는 툴킷.** 키트는 소유자가 쓰는 스킬, MCP 서버 템플릿, CLI 검사를 각각 출처와 함께 묶는다. 30행의 "번들하지 않는다"를 라이선스가 허락하는 곳에서만 뒤집는다. 스킬은 업스트림 라이선스가 재배포를 허락할 때만 원본 그대로 번들하고 LICENSE, 고정 업스트림 커밋, `THIRD_PARTY_NOTICES.md` 항목을 둔다. 공개 항목이어도 재배포 라이선스가 없으면 설치 명령과 URL만 연결한다. 소유자 전용·비공개 스킬, 계정 결속 커넥터, 제품 전용 도구는 제외한다. 키트는 `skills/`, `oah skills list/install/update`, `${ENV_VAR}` 자리표시자만 있는 `mcp/` 템플릿, 권장 CLI와 공식 설치 명령을 보여 주는 `oah doctor` 검사, 영어·한국어 툴킷 페이지를 제공한다. unlazy는 P5 계약 테스트 핀으로만 설치하고 게이트 명령 전에 커밋·스크립트 다이제스트를 검증한다(5.8). 자동 업데이트는 상태 폴더 키트 캐시만 갱신하며 추적 프로젝트 스킬 사본은 고정한다(A15). 목록은 아래에 있다. | 2, 3, 4(30행), 5.2, 5.8, 9 | P7, P8, P9 |
| A14 | **모델은 업데이트를 따라간다.** `oah`는 설정 기본값에 편집 가능한 엔진별 강한 순서의 `quality_order`를 제공한다. Claude 기본값은 `["opus", "sonnet", "haiku"]`로 Claude Code가 최신 모델로 해석하는 CLI 별칭을 쓴다. low는 마지막 가용 항목, medium은 두 번째(하나뿐이면 첫 번째), high와 max는 첫 번째를 쓴다. Codex는 모든 tier에서 실제 설정 모델로 시작하고 A1로 강도만 바꾼다. 사용자 품질 목록이 없으면 해석 가능한 CLI 보고 기본값이 자동 매핑을 대체하고, 그렇지 않으면 실제 설정 모델을 유지한다. 사용자 품질 목록은 Claude와 같은 tier 매핑을 쓴다. 새 Codex 모델은 CLI 모델 목록이 기본값으로 표시하거나 사용자 품질 목록에 이름이 있을 때만 채택한다. CLI 보고는 가용성과 지원 추론 단계를 제공하며 품질 순위를 추정하지 않는다. 우선순위는 명시적 tier 모델 핀, 자동 카탈로그 매핑, 5.5의 실제 설정 해석기 순이다. 새 설치에서 모델을 정할 수 없으면 여전히 `OAH_MODEL_UNRESOLVED`로 거절한다. 모델 업데이트는 실행 전마다 카탈로그를 읽고 최신 여부를 검사하며 CLI 버전 변경, TTL 만료, `oah models --refresh` 때 갱신한다. 검사 성공 뒤 다음 실행은 자격을 갖춘 매핑을 쓰며 A1이 선택 모델의 강도를 다시 정한다. 정한 모델이나 강도가 바뀔 때마다 이전 값, 새 값, 시점, 이유를 기록한다. 갱신이 실패하면 실패를 알리고 낮추지 않은 채 마지막 정상 매핑을 유지한다. 명시적 핀은 항상 우선한다. | 4(7행), 5.5 | P3 |
| A15 | **스킬을 자동 업데이트하고 자동 사용한다.** 업데이트 가능한 번들·연결 스킬은 허용 목록에 있는 업스트림과 설치 커밋을 기록하며 사용자가 명시적으로 고정한 것은 자동으로 올리지 않는다. unlazy는 운영 의존성이라 A15가 업데이트하지 않는다. 후보 커밋을 대상으로 P5 계약·게이트 무효화 테스트를 통과하는 변경으로만 채택한다(5.8). 자동 업데이트는 작업 트리 밖 `oah` 상태 폴더의 키트 캐시만 갱신하며 프로젝트가 직접 번들하지 않은 스킬을 제공한다. `.claude/skills/`나 `.agents/skills/`의 추적 프로젝트 사본은 핀이다. 자체 범위·리스를 가진 별도 작업에서 명시적인 `oah skills update --project`로만 바꾼다. 같은 스킬 ID의 선택 순서는 프로젝트 사본, 키트 캐시, 사용자·플러그인 사본이며 선택은 모든 사본을 읽기만 한다. 안내형 설정은 기본적으로 키트 캐시 자동 업데이트를 켜고 사용자가 끌 수 있다. `update_interval_days` 기본값은 1일이다. 쓰기를 하는 `oah` 명령은 저장소에 활성 리스가 없을 때 다음 작업 전에 주기당 최대 한 번 기한 검사를 하므로 작업을 계속하면 기본적으로 적어도 하루 한 번 검사한다. 유휴 검사부터 업데이트 게시까지 상태 잠금을 유지해 리스 획득과 경합하지 않게 한다. 활성 리스가 있으면 검사를 미룬다. 선택형 launchd(macOS)나 Task Scheduler(Windows) 항목은 동의를 받아야 설치·제거하며 같은 잠금 검사를 호출한다. 전역 스킬 폴더는 확인을 받는 명시적 `oah skills update --global`로만 갱신하고 예약 실행하지 않는다. 설치 커밋을 올리기 전에 라이선스를 확인하고 이전/새 커밋·시점·이유를 담은 diff 요약을 기록하며 스킬 로딩 검사와 개인정보 스캔을 실행한 뒤 원자적으로 게시한다. 실패하거나 의심스러운 업데이트는 마지막 정상 커밋으로 되돌리고 보고하며 조용히 넘기지 않는다. A9는 선택한 설치 버전을 쓰므로 업데이트 확인 뒤 다음 작업은 수동 절차 없이 자격을 갖춘 검증 업데이트를 쓴다. 프로젝트 핀과 명시적 사용자 핀은 바뀌지 않는다. | 4(30행), 5.10, 5.11, 6.5, 7 | P4, P7 |
| A16 | **소유자 하네스와 업스트림 동기화.** `docs/UPSTREAM_SYNC.md`는 원본 하네스에서 마지막으로 검토한 커밋과 검토한 모든 변경 표를 기록한다. 각 행은 커밋, 요약, 판정(범용 기능으로 이식, 비공개·소유자 전용이라 제외, 보류)을 담는다. 이식은 계획 항목과 PR을 명시한다. 단계 시작마다, 작업 중에는 적어도 하루 한 번 구현자가 새 커밋을 읽기 전용으로 읽고 모든 이식을 새로 작성하며, 이식 판정을 상시 수정 라운드 규칙에 따라 개정과 구현으로 옮긴다. 원본 하네스는 런타임 의존성이 되지 않는다. `oah` 사용자는 일반 릴리스로 업데이트를 받는다. | 1, 13 | 모든 단계, P9 문서 |
| A17 | **어디에도 자격 증명을 넣지 않는다**(엄격한 규칙). API 키, 토큰, 비밀번호, 쿠키, OAuth 시크릿, 계정 식별자는 저장소, 이력, 릴리스, 이슈, CI 로그 어디에도 넣지 않는다.<br>• 키는 문서화한 조회 경로 하나(`oah.secrets`)로 실행할 때만 환경 변수나 OS 키체인에서 읽는다. 키가 없으면 기능을 끈 채 `unused(no key)`를 보고하며 `oah`는 프로젝트 파일에 키를 붙여 넣으라고 요구하지 않는다.<br>• 템플릿은 `${ENV_VAR}` 자리표시자만 쓰고 문서도 자리표시자 이름만 보여 준다.<br>• 테스트는 실행 중 가짜 키를 생성하고 커밋한 픽스처에는 넣지 않는다.<br>• gitleaks와 `tools/privacy_scan.py`는 모든 pull request·push의 CI와 릴리스 태그 전 전체 이력에서 실행한다. 발견 항목이 있으면 빌드를 실패시키고 실제 자격 증명처럼 보이는 값은 허용 목록으로 제외하지 않는다.<br>• `oah init`은 사용자 프로젝트의 로컬 비밀값 파일을 git에서 무시하게 하고 문서에 키 보관 위치를 적는다.<br>• 소유자 컴퓨터에서 가져오는 것은 이름과 출처만 남긴다. | 2, 3, 8, 10 | 모든 단계, CI는 지금 적용 |
| OB | **초보자 온보딩**(6.5). OS별 한 줄 설치 프로그램이 안내형 `oah init`으로 넘긴다. 동의를 받아 공식 출처의 도구를 확인·설치하고 비밀값을 다루지 않으며 로그인을 안내하고 소유자 기본값으로 환경을 만든다. 이어 첫 안내형 작업(`examples/hello-oah`)을 실행하며 한국어·영어의 쉬운 메시지와 문서를 제공한다. | 6.5, 7 | P7, P8, P9 |

#### 개정 3 체크리스트

**P3**(P3b와 P3o 레인)
- [ ] 패킷에 tier가 없으면 `oah call`이 규칙으로 난이도 축을 채점하고 플래그 없이 `oah recommend`를 적용한다. `--tier`는 재정의하며 기록한다. (A10)
- [ ] 실행 기록에 축, tier와 출처(`rules`, `packet`, `flag`), 참고 판정(값과 함께 `used` 또는 `unused(<reason>)`), 요청·실제 모델과 강도를 보관한다. 참고 판정은 tier를 낮추지 않는다. 플러그인 부재, 키 부재, 플러그인 오류, 상향·하향 권고를 테스트한다. (A10)
- [ ] 스킬 선택기는 A15 우선순위에 따라 설치된 Claude·Codex 스킬 디렉터리(사용자, 프로젝트, 플러그인 스킬)와 키트 캐시를 읽기 전용으로 읽는다. 종류, 단계, 위험, Scope 파일 종류로 선택하며 스킬마다 이유를 붙이고 빈 Scope는 경고한다(A4). 브리프는 이름과 경로로 선택 스킬을 참조하거나 `[skills] mode = "attach"`이면 텍스트를 첨부한다. (A9)
- [ ] 재정렬기 플러그인 인터페이스는 순서만 바꾸며 `rerank=used` 또는 `rerank=unused(<reason>)`을 기록한다. TypeSafe Jev 제공자는 명시적 활성화와 `oah.secrets`를 통한 키가 모두 필요하다. 키는 있으나 동의하지 않은 경우(`unused(not-enabled)`), 켰으나 키가 없는 경우(`unused(no key)`), 브리프에서 끈 경우, 가짜 제공자와 네트워크 없이 동의받아 켠 호출을 테스트한다. (A9)
- [ ] 스킬 선택 사용 기록은 상태 폴더의 로컬 JSON-lines 파일이며 전송하지 않는다. 동의받아 켠 제공자만 A9에서 밝힌 데이터를 보낸다. 선택 문구는 종류, 단계, 위험, 가용성에 따른 선택을 설명하고 선택형 스킬을 하나도 쓰지 않는다고 약속하지 않는다. (A9, C6)
- [ ] `oah models [--refresh]`는 편집 가능한 엔진별 품질 목록과 A14의 정확한 tier 매핑을 쓴다. Claude 별칭, Codex 실제 기본값, CLI 보고 기본값 채택과 사용자 목록 모델, 카탈로그 매핑보다 우선하는 명시적 tier 핀, 실제 설정 대체 경로, `OAH_MODEL_UNRESOLVED`를 테스트한다. 실행 전마다 모델 업데이트를 확인하고 카탈로그 최신 여부를 검사하며 CLI 버전 변경, TTL 만료, `oah models --refresh` 때 갱신한다. 검사 성공 뒤 다음 실행은 자격을 갖춘 매핑을 쓴다. A1이 지원 강도를 다시 정한다. 변경마다 이전 값, 새 값, 시점, 이유를 기록한다. 갱신 실패는 경고하고 낮추지 않은 채 마지막 정상 매핑을 유지한다. (A14)
- [ ] `oah.secrets.get(name)`을 유일한 키 조회 경로로 쓴다. 환경 변수 우선, 다음은 OS 키체인(macOS `security`, Windows Credential Manager)이다. 키가 없으면 `unused(no key)`를 보고한다. 키는 로그, 실행 기록, 브리프에 들어가지 않는다. (A17)
- [ ] Orca가 설치돼 있으면 선택형 Orca 어댑터가 `oah call` 워커를 Orca로 배정한다. (8행, P3o 레인)

**P4**(P4b 레인)
- [ ] `oah packet new`가 규칙으로 축을 채점하고 축, 권장 tier, 선택 스킬과 이유를 `## Plan`에 기록한다. (A9, A10)
- [ ] `oah preflight`와 `oah packet new`는 먼저 영향 지도를 갱신하거나 `OAH_IMPACT_STALE`로 거절한다. (A11)
- [ ] 브리프와 패킷은 A15 선택 우선순위를 써서 업데이트 확인 뒤 다음 작업이 자격을 갖춘 검증 캐시 업데이트를 사용하게 한다. 프로젝트 핀과 명시적 사용자 핀은 바뀌지 않는다. (A15)

**P5**
- [ ] `oah hooks install`이 선택형 `post-commit`, `post-merge` git 훅을 제안하며 Stop 훅과 같은 diff 표시, 백업, 제거 규칙을 쓴다. (A11)
- [ ] `oah hook pre-push`는 이 워크트리에 끝나지 않은 리스가 있으면 거절하고, gitleaks가 설치돼 있으면 push할 커밋에서 실행한다. (3행, A17)
- [ ] `oah init --with-unlazy`는 P5 계약 테스트를 거친 커밋과 스크립트 다이제스트로 설치한다. 모든 게이트 명령은 환경/프로젝트/사용자 위치에서 찾은 사본을 실행 전에 두 식별 정보와 대조한다. 불일치 테스트는 거절, 정확한 고정 설치 명령, 재정의 제거 안내를 확인한다(5.8). 핀은 A15에서 제외한다. 후보 커밋을 채택하려면 그 커밋에서 CLI 인자, 상태, 증거 동작을 다루는 어댑터 계약·게이트 무효화 테스트가 필요하다. `gates check`는 커밋과 리뷰 전에 경로 없는 요약을 쓰고 `finish`는 추적 파일을 쓰지 않고 검증한다. (A15, 5.8)

**P6**
- [ ] `oah review`는 먼저 영향 지도를 갱신하거나 `OAH_IMPACT_STALE`로 거절한다. (A11)
- [ ] `oah land --check`는 수렴한 리뷰 영수증, CI check-run(`gh`가 있으면 사용), 스캔, 게이트가 정확한 head에 결속했는지 검증한다. `--merge`는 사용자 확인 뒤에만 반영한다. (29행)

**P7**(P7a, P7i, P7t, P7b 레인)
- [ ] `oah impact build/refresh`는 A11의 실제 입력 명세로 워크트리별 지도를 저장한다. HEAD, 추적 인덱스, 변경/미추적 입력, 무시된 원장, 설정, 도구 버전 변경으로 무효화되는지, HEAD를 공유하는 워크트리를 분리하는지 테스트한다. 오래된 지도는 갱신하거나 거절한다. 기본 SQL 플러그인은 코드 참조에 연결된 `db_table`, `db_routine`, `db_grant` 개체를 만든다. `oah init`은 첫 지도를 만든다. 실패하면 `impact=unavailable`로 경고하고 나머지 init은 성공으로 남긴다. dry-run은 빌드를 나열만 하고 두 번째 실행은 오래된 지도를 갱신하거나 아무것도 바꾸지 않는다. 편집 시점 영향 힌트는 선택형이다. (A11)
- [ ] `oah init`은 Obsidian 호환 위키를 만든다. 색인, 마커가 있는 로그, 템플릿, workspace 파일을 무시하는 최소 `.obsidian/`다. 리드나 안내형 기본 절차가 최종 커밋, 게이트, 리뷰 전에 로그/색인/분할 보관용 `wiki maintain --task T`를 실행한다. 범위 예외 출력 이벤트, 잠금 경합, 분할 보관/복구를 포함한 멱등 재시도, 재정의 기록 없이는 누락·오래된 유지 관리를 finish가 거절하는지 테스트한다. `finish`와 pre-commit의 린트·정리 상태 검사는 추적 파일을 쓰지 않는다. 그래프를 자동 재구축하지 않는다. (A12)
- [ ] `skills/`에는 검증한 라이선스가 재배포를 허락하는 스킬만 두며 각각 원본 그대로 LICENSE, 업스트림 URL, 고정 커밋, `THIRD_PARTY_NOTICES.md` 항목을 갖춘다. 연결 스킬은 `oah skills install`로 설치한다. unlazy는 P5 계약 테스트 핀으로 설치하고 커밋·스크립트 다이제스트를 검증한다(5.8). 추적 프로젝트 사본은 A15에 따라 고정한다. `mcp/` 템플릿은 `${ENV_VAR}` 자리표시자만 쓴다. (A13)
- [ ] 안내형 설정은 기본적으로 키트 캐시 스킬 자동 업데이트를 켜며 사용자가 끌 수 있다. 자동으로 쓸 수 있는 곳은 작업 트리 밖 상태 폴더의 키트 캐시뿐이며 프로젝트가 번들하지 않은 스킬을 제공한다. 선택 순서는 프로젝트 사본, 키트 캐시, 사용자·플러그인 사본이다. 추적하는 `.claude/skills/`, `.agents/skills/` 사본은 핀이며 자체 범위·리스를 가진 별도 작업의 명시적 `oah skills update --project`로만 바꾼다. 자동 검사가 추적 프로젝트 폴더를 바꾸지 않고 프로젝트 업데이트가 별도 리스·범위를 강제하는지 테스트한다. 기본값 `update_interval_days = 1`, 유휴 중 쓰기 명령을 통한 다음 작업 전 주기당 최대 한 번 기한 검사, 업데이트 중 리스가 시작되지 않도록 게시까지 상태 잠금 유지도 테스트한다. 선택형 launchd/Task Scheduler 항목의 설치·제거는 동의가 필요하다. 전역 업데이트는 확인을 받는 `skills update --global`이 필요하며 예약 실행하지 않는다. 업데이트는 사용자 핀을 지키고 unlazy를 제외하며 라이선스/로딩/개인정보를 검증한다. 이전/새 커밋·시점·이유와 diff를 기록하고 원자적으로 게시하거나 되돌린 뒤 실패를 보고한다. (A15)
- [ ] `oah init`은 안내형 설정이다. 카탈로그로 도구를 검사하고 사용자가 확인한 것만 설치하며 비밀값을 다루지 않고 Claude·Codex 로그인을 안내한다. 소유자 작업 흐름과 같은 기본값으로 규칙 블록, tier 에이전트, 위키, 영향 지도, 게이트 템플릿, 수락한 훅을 쓴다. (OB)
- [ ] 안내형 `oah init`은 제공자 활성화를 한 번 묻고 발생할 수 있는 비용과 전송할 후보/증거 데이터를 밝히며 답을 기록한다. 키만 있거나 도구 설치용 `--yes`에 동의했다고 제공자를 켜지 않는다. 설정을 통한 활성화도 지원한다. (A9, AC6)
- [ ] `oah handoff`와 선택형 SessionStart 훅을 제공한다. 훅은 `OAH_WORKER=1`이면 출력하지 않는다. (11행)
- [ ] `oah init`은 로컬 비밀값 파일용 `.gitignore` 항목을 추가한다. `.env`, `.env.*`, `*.pem`, `*.key`, `.oah/secrets*`, 로컬 MCP 파일이다. (A17)

**P8**
- [ ] OS마다 명령 하나로 `oah`를 설치하고 안내형 설정으로 넘긴다. `oah.setup`의 도구 카탈로그는 도구마다 검사, OS별 공식 설치 명령, 공식 URL을 나열하며 동의 없이는 설치하지 않는다. (OB)
- [ ] 비공개 README 방식은 클론에서 실행하며 git이 사전 요구 사항임을 밝힌다(`xcode-select --install` 또는 `winget install --id Git.Git -e`). 공개 방식은 `curl … | bash` 또는 `irm … | iex`로 git 없이 고정 릴리스 압축 파일을 내려받고 압축 해제 전에 SHA-256을 검증한다. uv와 `oah`를 설치하고 동의를 받아 git을 제안한다. 릴리스 URL/다이제스트는 공개 때까지 자리표시자로 남긴다. git 부재, 다이제스트 거절, 새 터미널의 `oah --version`을 검사한다. (OB, AC1)
- [ ] `oah doctor`는 같은 카탈로그의 권장 CLI를 검사하고 공식 설치 명령을 출력한다. (A13)

**P9**
- [ ] 문서는 스킬 자동화, 난이도, 영향 지도, 위키, 출처가 있는 툴킷, 모델·스킬 업데이트, 업그레이드 경로(A16), 사용자 키 보관 위치(A17)를 영어와 한국어로 다룬다. 툴킷 페이지는 모든 항목에 출처와 라이선스를 적고 `examples/hello-oah`는 전체 과정을 안내한다. (A9–A17, OB)

**릴리스**
- [ ] 모든 단계를 병합하고 macOS·Windows CI가 통과했다.
- [ ] 전체 이력의 개인정보 스캔과 gitleaks가 깨끗하다.
- [ ] README 상태 상자의 "developer preview"를 갱신했다.
- [ ] 릴리스 노트가 있는 `v0.1.0` 태그를 만들었다.
- [ ] 저장소 공개나 사람 초대를 준비하고 결정은 소유자에게 남겼다.

#### 레인과 의존성

| 레인 | 내용 | 필요한 선행 작업 |
|---|---|---|
| P3a | 설정 로더, 명령 탐색, 패키지 데이터 | P2 |
| P3b | 라우팅, 엔진, `call`, 실행, 브리프. A9, A10, A14, A17 조회 | P3a, P4a |
| P3o | 선택형 Orca 어댑터 | P3b |
| P4a | 패킷, 의도(공유 푸터 포함), 사전 점검 핵심 | P2 |
| P4b | 호출 거절/푸터 통합을 포함한 `start`, `status`, `finish`, `call`의 모든 의도 연결. `packet new`의 A9·A10, A11 갱신 | P3b, P4a, P5, P7i |
| P5 | 게이트, 검증한 unlazy 커밋/스크립트 핀과 계약, `check` 도우미, `finish` 가드, Stop·git 훅 | P2 |
| P6 | 리뷰 루프, 반영 검사, A11 갱신. P4b의 최신 의도 결속 사용 | P3b, P4b, P5, P7i |
| P7a | 위키(A8, A12), 규칙 블록, 그래프 | P2 |
| P7i | 영향 지도(A11), SQL 플러그인 | P2, 이후 게이트 결속에 P5 |
| P7t | 툴킷(A13), 캐시 전용 스킬 자동 업데이트와 별도 리스의 `--project` 업데이트(A15), 고정 unlazy 설치 | P3a, unlazy 핀·계약에 P5 |
| P7b | 안내형 `oah init`(OB), tier, 훅 연결, `handoff`, SessionStart | P3b, P4b, P5, P7a, P7i, P7t, P8, P7c, P8c |
| P7c | 사용자 프로젝트 CI 스타터(A21). `oah ci init`과 템플릿 | P3a |
| P8 | 설치 프로그램, 도구 카탈로그, `doctor` 도구 검사 | P3a |
| P8c | 선택형 동반 앱 Orca·Aside(A20). 설정 단계, MCP 등록, 규칙 절, `doctor` 항목 | 연결에 P8 필요. 자체 모듈은 먼저 시작 가능 |
| P7j | 기본 브랜치 영향 갱신(A23) | P7i |
| P9 | 문서, 예제, 문제 해결, 툴킷 페이지 | 모두 |

모든 레인은 P3a의 탐색으로 명령을 등록하고 `oah.config`로 설정을 읽는다. P3a 반영 전에
시작한 레인은 리뷰 전에 그 위로 rebase한다.

#### A13 툴킷 목록(2026-10-23)

이 목록은 소유자 컴퓨터에서 읽기 전용으로 수집했다. 업스트림 LICENSE 파일에서 라이선스를 검증했다.
고정 커밋의 로컬 업스트림 사본으로 확인한 경우처럼 검증이 약한 곳은 번들하는 레인이
먼저 고정 커밋의 LICENSE를 다시 읽는다.

각 행은 네 가지 판정 가운데 하나다.
- **vendor**는 LICENSE, 고정 커밋, 고지와 함께 원본 그대로 번들한다. 자동 업데이트는 프로젝트가
  번들하지 않은 스킬의 상태 폴더 키트 캐시만 갱신한다. 추적 프로젝트 사본은 자체 범위·리스를
  가진 작업에서 `oah skills update --project`를 실행할 때까지 고정한다(A15).
- **template**(MCP 서버)은 `${ENV_VAR}` 자리표시자만 있는 실행 템플릿을 제공한다.
- **link**는 공식 설치 명령과 URL만 제공한다.
- **exclude**는 제공하지 않는다.

비공개 용어와 일치하는 이름은 가렸다. 값(환경 변수, 헤더, 토큰)은 기록하지 않았다.

**실제 배포 판정(2026-10-24).** P7t 레인(PR #19)은 처음에 `vendor`로 표시했던 세 행을
최종적으로 `link`로 정했다. unlazy와 superpowers는 업스트림 파일이 그대로는 이 저장소의
개인정보 스캔을 통과하지 못하며 번들 파일을 편집하지 않으므로 번들 대신 연결한다.
graphify는 업스트림에 스킬 폴더가 없고 CLI가 템플릿에서 스킬을 쓰므로 연결한다.
`skills/`에는 `THIRD_PARTY_NOTICES.md`에 적힌 번들 항목 아홉 개가 있다.
아래 `vendor` 여덟 행(mattpocock-skills 포함)에 P7t가 추가한 선택형 `humanize-korean`을 더한 것이다.
`docs/toolkit.md`에 연결 항목마다 설치 명령과 이유를 적는다.

##### 스킬

| 종류 | 이름 | 용도 | 업스트림 | 버전/커밋 | 라이선스 | 판정 |
|---|---|---|---|---|---|---|
| skill | computer-use | Orca CLI로 눈에 보이는 로컬 앱 창의 GUI 조작(접근성 트리, 클릭, 입력, 스크린샷). | <https://github.com/stablyai/orca/tree/a0cd1644f320f1327f7495650a816111b495f3cd/skills/computer-use> | a0cd1644f320f1327f7495650a816111b495f3cd | MIT | vendor |
| skill | orca-cli | orca CLI로 Orca 워크트리, 터미널, 저장소, 내장 브라우저, 인계 조작. | <https://github.com/stablyai/orca/tree/a0cd1644f320f1327f7495650a816111b495f3cd/skills/orca-cli> | a0cd1644f320f1327f7495650a816111b495f3cd | MIT | vendor |
| skill | orchestration | 감독받는 Orca 워커 에이전트 조율. 메시지, 작업 배정, DAG, 결정 게이트. | <https://github.com/stablyai/orca/tree/a0cd1644f320f1327f7495650a816111b495f3cd/skills/orchestration> | a0cd1644f320f1327f7495650a816111b495f3cd | MIT | vendor |
| skill | typesafe-ai | TypeSafe 타입 기반 AI 판정 기본 요소로 기능을 만드는 지침(라우팅, 순위, 검증). | <https://github.com/typesafe-ai/skills/tree/main/skills/typesafe-ai> | lock에 커밋 기록 없음(폴더 해시 678c375b883794bf4292715a43acbba5c84681aa). 확인 시 업스트림 main HEAD는 65a39f393687675ce170e6094757de20370365b9 | MIT | vendor |
| skill | unlazy | 완료 규율과 운영 게이트 검사기. P5 계약 테스트 핀으로 설치하고 게이트 명령 전에 커밋·스크립트 다이제스트 검증(5.8). A15 업데이트 제외. | <https://github.com/Leonxlnx/unlazy> | 2.1.0(package.json). lock 해시 16671491f6679ad9378f52604d3bc2415b4120c7은 확인 시 업스트림 main HEAD와 같음 | MIT | link |
| skill | vercel-react-best-practices | Vercel Engineering의 React·Next.js 성능 규칙. | <https://github.com/vercel-labs/agent-skills/tree/main/skills/react-best-practices> | 스킬 메타데이터 1.0.0. lock에 커밋 기록 없음. 확인 시 업스트림 main HEAD는 063bee94c3f4df8453406c830b0a7df0f2860278 | NOASSERTION | link |
| skill | graphify | 코드와 문서를 query, path, explain 도구가 있는 영속 지식 그래프로 변환. | <https://github.com/Graphify-Labs/graphify> | graphifyy 0.9.93(PyPI), 업스트림 커밋 미기록 | Apache-2.0 | link |
| skill | [private-term skill A] | 제외. 비공개 용어와 일치 | - | - | - | exclude |
| skill | [private-term skill B] | 제외. 비공개 용어와 일치(계정 동기화 사본) | - | - | - | exclude |
| skill | [owner-specific article skill] | 제외. 소유자 전용이며 비공개 용어와 일치 | - | - | - | exclude |
| skill | docx | Word 문서 생성·읽기·편집. | <https://github.com/anthropics/skills/tree/main/skills/docx> | 계정 동기화 사본. 확인 시 업스트림 main HEAD는 dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | pdf | PDF 읽기·병합·분할·양식 채우기·생성. | <https://github.com/anthropics/skills/tree/main/skills/pdf> | 계정 동기화 사본. 확인 시 업스트림 main HEAD는 dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | pptx | PowerPoint 발표 자료 생성·읽기·편집. | <https://github.com/anthropics/skills/tree/main/skills/pptx> | 계정 동기화 사본. 확인 시 업스트림 main HEAD는 dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | xlsx | 스프레드시트 생성·읽기·편집. | <https://github.com/anthropics/skills/tree/main/skills/xlsx> | 계정 동기화 사본. 확인 시 업스트림 main HEAD는 dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | skill-creator (Anthropic) | 발동 설명 조정을 포함한 에이전트 스킬 생성·편집·평가. | <https://github.com/anthropics/skills/tree/main/skills/skill-creator> | 계정 동기화 사본. 확인 시 업스트림 main HEAD는 dbd4588f9e1033efb41dad4bef2f7947c8993d44 | Apache-2.0 | vendor |
| skill | built-in-browser | Claude 데스크톱 앱의 내장 브라우저 창 사용. | - | 계정 동기화 | LicenseRef-Anthropic-Proprietary | exclude |
| skill | chrome-browser | Claude in Chrome 확장 사용. | - | 계정 동기화 | LicenseRef-Anthropic-Proprietary | exclude |
| skill | computer-use (Anthropic) | Claude 데스크톱 앱으로 데스크톱 제어. | - | 계정 동기화 | LicenseRef-Anthropic-Proprietary | exclude |
| skill | docs | Claude Docs 문서. | - | 계정 동기화 | NOASSERTION | exclude |
| skill | google-workspace | 계정 커넥터로 Google Docs/Sheets/Slides 편집. | - | 계정 동기화 | NOASSERTION | exclude |
| skill | morning | 계정 데이터로 아침 브리핑 작성. | - | 계정 동기화 | NOASSERTION | exclude |
| skill | import-memory | 다른 어시스턴트가 내보낸 기억을 Claude 기억으로 가져오기. | - | 계정 동기화 | NOASSERTION | exclude |
| skill | deep-research | 리서치 서브에이전트를 조율해 보고서 작성. | - | 계정 동기화 | NOASSERTION | exclude |
| skill | Codex 시스템 스킬(imagegen, openai-docs, review-agent, skill-creator, skill-installer) | Codex CLI에 포함된 스킬. | - | Codex system-skills 마커 3eadc798bef12c4a | Apache-2.0 | exclude |
| skill | Claude Code 내장 스킬(artifact-*, update-config, keybindings-help, code-review, simplify, loop, schedule, claude-api, init, security-review, run, plugin-authoring, workflow-authoring, dataviz, claude-in-chrome, fewer-permission-prompts) | Claude Code 또는 claude.ai에 포함된 스킬. | - | Claude Code 2.1.309 | LicenseRef-Anthropic-Commercial | exclude |

##### 플러그인

| 종류 | 이름 | 용도 | 업스트림 | 버전/커밋 | 라이선스 | 판정 |
|---|---|---|---|---|---|---|
| plugin | superpowers@claude-plugins-official | 작업 흐름 스킬. 브레인스토밍, 계획, TDD, 디버깅, 코드 리뷰, 워크트리, 검증. | <https://github.com/obra/superpowers> | 7.0.0 @ bb92a77741419a4ab5f06e711a283343f1ada0c3 | MIT | link |
| plugin | mattpocock-skills | 엔지니어링·생산성 스킬. TDD, 진단, 도메인 모델링, 코드 리뷰, PR 작성, 질문으로 검토. | <https://github.com/mattpocock/skills> | 1.3.1 @ 49dd158d1076134a641b33efb035946536778336 | MIT | vendor |
| plugin | codex@openai-codex | 막힌 작업 해결, 리뷰, 위임 작업을 위해 Codex CLI를 호출하는 Claude Code 플러그인. | <https://github.com/openai/codex-plugin-cc> | 1.0.6 @ db52e28f4d9ded852ab3942cea316258ae4ef346 | Apache-2.0 | vendor |
| plugin | ponytail@ponytail | 'Lazy senior dev'(게으른 시니어 개발자) 모드. 에이전트가 코드를 덜 쓰도록 유도하는 스킬과 감사/부채/리뷰 도우미. | <https://github.com/DietrichGebert/ponytail> | 5.3.2, 마켓플레이스 개정 2ed6c52c9d7e5e56942508591085fd45dea277d3 | MIT | vendor |
| plugin | omo@sisyphuslabs (LazyCodex) | Codex 하네스 플러그인. 에이전트 역할, ultrawork/loop 스킬, LSP·코드 그래프 도구, 수명 주기 훅. | <https://github.com/code-yeongyu/oh-my-openagent> | 5.1.40 | LicenseRef-Sustainable-Use-1.0 | link |
| plugin | cowork-plugin-management | Claude Cowork 플러그인 생성·맞춤 설정. | <https://github.com/anthropics/knowledge-work-plugins/tree/main/cowork-plugin-management> | 계정 동기화 버전 0040. 업스트림 plugin-dir 마지막 커밋 e7a5e58b7d08dbadf66e4121b06c449e4faa2fb4 | Apache-2.0 | exclude |
| plugin | Codex openai-bundled 플러그인(browser, chrome, computer-use, unified-computer-use, codex-app-tools, code-review, visualize) | Codex 데스크톱 앱에 포함된 플러그인. | - | 로컬 내장 마켓플레이스 | NOASSERTION | exclude |
| plugin | Codex openai-primary-runtime 플러그인(documents, presentations, spreadsheets, pdf, template-creator) | Codex 앱에 포함된 오피스 파일 런타임. | - | 26.1017.11052(런타임 캐시) | NOASSERTION | exclude |
| plugin | Codex openai-curated 커넥터(github, notion) | 계정에 결속한 앱 커넥터. | - | - | NOASSERTION | exclude |

##### MCP 서버

| 종류 | 이름 | 용도 | 업스트림 | 버전/커밋 | 라이선스 | 판정 |
|---|---|---|---|---|---|---|
| mcp | context7 | 최신 라이브러리·프레임워크 문서 가져오기. | <https://github.com/upstash/context7> | @upstash/context7-mcp@latest로 실행(npx 캐시에 4.4.4와 2.2.5). 확인 시 업스트림 master HEAD는 d980242a5b04be30ae8804124a8be259bcc2a227 | MIT | template |
| mcp | korean-law | 한국 법령·판례 검색과 읽기. | <https://github.com/chrisryugj/korean-law-mcp> | korean-law-mcp@latest로 실행(npx 캐시 4.15.9). 확인 시 업스트림 main HEAD는 0a8f08ef4f697e08047f76bfab8be435f5bdda5a | MIT | template |
| mcp | openai (mcp-openai) | 에이전트가 도구로 OpenAI 채팅 모델에 질의하게 함. | <https://github.com/mzxrai/mcp-openai> | @mzxrai/mcp-openai@latest로 실행(npx 캐시 0.1.1) | MIT | template |
| mcp | aside | 웹 검색, 페이지 읽기, 사이트 작업용 브라우저 에이전트. | <https://aside.com/> | CLI 1.26.1010.1739 | LicenseRef-Proprietary | link |
| mcp | openaiDeveloperDocs | 호스팅되는 OpenAI 개발자 문서 MCP 엔드포인트. | <https://developers.openai.com/mcp> | 호스팅 서비스 | NOASSERTION | link |
| mcp | node_repl | ChatGPT 데스크톱 앱에 포함된 Node REPL 도구. | - | - | NOASSERTION | exclude |
| mcp | computer-use (Codex) | Codex Computer Use 데스크톱 제어 클라이언트. | - | - | NOASSERTION | exclude |
| mcp | claude-in-chrome | Claude in Chrome 브라우저 확장 도구. | - | - | LicenseRef-Anthropic-Commercial | exclude |
| mcp | 계정 결속 커넥터 | 메일, 일정, 드라이브, 오피스 제품군, 문서, 미디어 생성 범주의 claude.ai 커넥터. | - | - | - | exclude |

##### CLI

| 종류 | 이름 | 용도 | 업스트림 | 버전/커밋 | 라이선스 | 판정 |
|---|---|---|---|---|---|---|
| cli | git | 버전 관리. | <https://github.com/git/git> | 2.50.1(Apple Git-155) | GPL-2.0-only | link |
| cli | gh | PR, 이슈, API 호출을 위한 GitHub CLI. | <https://github.com/cli/cli> | 2.91.0 | MIT | link |
| cli | node | MCP 서버와 훅을 위한 JavaScript 런타임. | <https://github.com/nodejs/node> | v24.6.0 | MIT | link |
| cli | npm | Node 패키지 관리자. | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link |
| cli | npx | 설치하지 않고 npm 패키지 실행(MCP 실행기). | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link |
| cli | pnpm | 빠른 Node 패키지 관리자. | <https://github.com/pnpm/pnpm> | 10.32.1 | MIT | link |
| cli | uv | Python 패키지·도구 관리자. | <https://github.com/astral-sh/uv> | 0.12.17 | MIT OR Apache-2.0 | link |
| cli | python3 | Python 런타임. | <https://github.com/python/cpython> | 3.14.4 | PSF-2.0 | link |
| cli | claude | Claude Code CLI. | <https://github.com/anthropics/claude-code> | 2.1.309 | LicenseRef-Anthropic-Commercial | link |
| cli | codex | OpenAI Codex CLI. | <https://github.com/openai/codex> | codex-cli 0.163.0 | Apache-2.0 | link |
| cli | gitleaks | git 이력과 스테이징 변경의 비밀값 스캐너. | <https://github.com/gitleaks/gitleaks> | 8.30.1 | MIT | link |
| cli | graphify | graphify 스킬 뒤에서 동작하는 지식 그래프 CLI. | <https://github.com/Graphify-Labs/graphify> | 0.9.93(PyPI graphifyy) | Apache-2.0 | link |
| cli | aside | Aside 브라우저 에이전트 CLI. | <https://aside.com/> | 1.26.1010.1739 | LicenseRef-Proprietary | link |
| cli | orca | 워크트리, 터미널, 에이전트 오케스트레이션용 Orca CLI. | <https://github.com/stablyai/orca> | 1.4.237 | MIT | link |
| cli | jq | JSON 처리기. | <https://github.com/jqlang/jq> | 1.8.1 | MIT | link |
| cli | rg | ripgrep 빠른 검색. | <https://github.com/BurntSushi/ripgrep> | 15.2.0 | Unlicense OR MIT | link |

목록에 있는 unlazy 커밋은 원본에서 관측한 정보이며 테스트하지 않은 검사기를 설치해도 된다는 뜻이 아니다.
P5가 운영 커밋과 스크립트 다이제스트를 계약 테스트하고 기록한다. init은 그 핀을 설치하고,
탐색은 불일치를 거절하며 5.8의 복구 명령을 제시한다. 새 커밋에는 P5 계약 변경이 필요하다.

목록에서 남은 열린 질문이다. 해당 항목은 모두 번들이 아닌 연결이므로 어느 것도 진행을 막지 않는다.
- `vercel-react-best-practices`의 README는 MIT라고 하지만 업스트림 저장소에 LICENSE 파일이 없다.
  업스트림에 파일이 추가될 때까지 연결만 한다.
- LazyCodex 플러그인의 로컬 메타데이터는 MIT라고 하지만 업스트림 LICENSE는 Sustainable Use License
  1.0이므로 연결만 한다.
- graphify는 Apache-2.0과 MIT 라이선스 파일을 모두 제공하며 어느 쪽도 스킬 번들을 허락한다.
  담당 레인은 번들하는 릴리스의 업스트림 커밋을 고정한다.
- `typesafe-ai`의 로컬 lock에는 커밋이 없으므로 담당 레인이 번들할 업스트림 커밋을 고정한다.
- Aside는 비공개 소스다. 선택 사항으로 연결하고 엔진의 내장 웹 도구를 기본값으로 유지한다(34행).

### 개정 4(2026-10-23)

개정 3 뒤 소유자가 하네스에 적용한 변경 두 개를 A16에 따라 이식했다
(`docs/UPSTREAM_SYNC.md`의 `82492ea30` 행). 5.6과 5.10을 갱신했다.
일관성도 고쳤다. `oah impact`는 공유 CLI와 argparse 관례에 맞춰 사용법 오류에 종료 코드 2를 쓴다.

| # | 규칙 | 절 | 단계 |
|---|---|---|---|
| A18 | **더 안전한 로그 분할 보관.** `oah wiki rollover`는 계획을 출력하는 모의 실행이다. 쓰기는 `--apply`로 하며 로그와 새 보관 파일을 범위에 담는 리스가 필요하다. `oah wiki maintain`은 리스 없이 위키 잠금 아래에서 분할 보관을 적용한다. 저널을 제거하기 전에 두 파일이 정확히 게시한 바이트여야 한다. 아니면 위키 파일을 그대로 두고 현재 바이트를 `changed-after-publication` 사유로 저널에 기록한다. 명령은 `OAH_WIKI_RECOVERY_PENDING`, 복구는 `OAH_WIKI_RECOVERY_CONFLICT`로 거절하며 다른 파일과 저널을 명시한다. 모든 복구 충돌에서 사용자는 파일을 수동 조정한 뒤 `oah wiki recover --resolved`를 실행한다. 로그와 모든 보관 파일이 중복 항목 없이 파싱되는지 검사하고, 저널의 원래 스냅샷이나 수집한 현재 바이트에 있지만 조정한 파일에 없는 항목을 정확한 바이트 비교로 모두 나열한다. 누락 항목은 해결 처리를 막지 않는다. 저널을 바이트 그대로 상태 폴더의 `resolved-records/`에 타임스탬프가 있는 해결 기록으로 옮기고 해결 시각·기록 경로를 이벤트에 기록한다. `--resolved`는 저널 바이트를 삭제하지 않는다. 검사 실패는 `OAH_WIKI_RECOVERY_CONFLICT`로 거절하고 아무것도 바꾸지 않는다. 모든 `OAH_WIKI_LOG_INVALID` 거절은 해당 경우를 명시하며 쓰기 전에 일어난다. 실행 기준과 목표만 `[wiki]` 설정값이다. 범위 예외, 린트, 복구를 위해 보관 이름은 `log_archive_NN.md`로 유지한다. | 5.10 | P7(P7a 레인) |
| A19 | **결속한 의도는 바꿔 끼울 수 없다.** 후속 라운드는 첫 배정 때 결속한 의도 원본을 다시 쓰며 원본이 다른 브리프는 `OAH_INTENT_SOURCE_MISMATCH`로 거절한다. 의도 검사 전에 워크트리 선택자는 정확히 한 저장소로 해석돼야 한다. 모호하거나 해석할 수 없으면 실행 전에 `OAH_WORKTREE_UNRESOLVED`와 실행 가능한 재시도 명령으로 거절한다. | 5.6 | P3(P3b 레인) |

#### 개정 4 체크리스트

**P3**(P3b 레인)
- [ ] 후속 라운드는 결속한 의도 원본을 다시 쓰며 원본이 다른 브리프는 `OAH_INTENT_SOURCE_MISMATCH`로 거절한다. (A19)
- [ ] 모호하거나 해석할 수 없는 워크트리 선택자는 실행 전에 `OAH_WORKTREE_UNRESOLVED`와 실행 가능한 재시도 명령으로 거절한다. (A19)

**P7**(P7a 레인)
- [ ] `oah wiki rollover`는 `--apply` 없이는 쓰지 않으며, 범위를 포함하는 리스 없이 `--apply`를 주면 `OAH_WIKI_LEASE_REQUIRED`로 거절한다. (A18)
- [ ] 게시 후 검사는 두 파일이 정확히 게시한 바이트일 때만 저널을 제거한다. (A18)
- [ ] 늦은 추가를 포함한 게시 후 차이가 있으면 위키 파일을 그대로 두고 현재 바이트를 `changed-after-publication` 사유로 저널에 기록하며 명령을 `OAH_WIKI_RECOVERY_PENDING`으로 거절한다. (A18)
- [ ] 복구는 `changed-after-publication` 저널이나 그 밖의 복구 충돌을 `OAH_WIKI_RECOVERY_CONFLICT`로 거절하고 다른 파일과 저널을 명시하며 모든 파일과 저널을 보존한다. (A18)
- [ ] 어느 충돌 경로든 수동 조정 뒤 `oah wiki recover --resolved`를 실행했을 때 로그와 모든 보관 파일이 파싱되고 중복 항목이 없으면 저널을 바이트 그대로 상태 폴더의 `resolved-records/` 아래 타임스탬프가 있는 해결 기록으로 옮긴다. 저널 바이트를 삭제하지 않는다. (A18)
- [ ] 성공한 `oah wiki recover --resolved`는 해결 시각과 해결 기록 경로를 이벤트에 기록한다. (A18)
- [ ] 조정 중 원래 항목이나 수집한 늦은 추가 항목을 빼면 정확한 항목 바이트 비교로 누락 항목을 각각 나열하고 `--resolved`를 막지 않으며 해결 기록에서 뺀 각 항목을 복구할 수 있게 남긴다. (A18)
- [ ] 파싱에 실패하거나 항목이 중복되면 `oah wiki recover --resolved`는 `OAH_WIKI_RECOVERY_CONFLICT`로 거절하고 아무것도 바꾸지 않는다. (A18)
- [ ] 잘못된 로그 거절은 모두 쓰기 전에 해당 경우를 명시한다. (A18)
- [ ] `[wiki]`에서 가져오는 것은 실행 기준과 목표뿐이다. 범위 예외, 린트, 복구를 위해 보관 이름은 `log_archive_NN.md`로 유지한다. (A18)

### 개정 5(2026-10-23)

개정 4 뒤 소유자의 요청 세 가지와 소유자 하네스에서 이식한 규칙 수정 하나(A23)다.
초보자는 push할 때 CI와 안전한 기본값을 놓치기 쉽고, 소유자의 작업 흐름은 동반 앱 둘에
의존하며, 소유자는 그 작업 흐름에서 고정 리뷰 라운드 상한을 없앴다.
4절(22행), 5.6, 5.9, 5.10, 6.5, 14절과 레인 표를 갱신했다.

| # | 규칙 | 절 | 단계 |
|---|---|---|---|
| A20 | **선택형 동반 앱 Orca와 Aside.** 안내형 설정은 선택 앱 둘을 각각 추가 기능 한 줄과 함께 제안한다. 사용자가 확인한 것만 제공자의 공식 출처에서 설치한다.<br>• **Orca**(Stably AI)는 격리된 git 워크트리에서 코딩 에이전트를 나란히 실행한다. macOS는 `brew install --cask stablyai/orca/orca` 또는 공식 DMG이며 관련 없는 `orca` cask를 쓰지 않는다. Windows는 `winget install --id StablyAI.Orca -e` 또는 공식 설치 프로그램이다. Linux는 공식 패키지 링크를 준다. 사용자가 Orca 설정에서 `orca` CLI를 켜면 설정 절차가 `orca --version`으로 검증한다. 동의를 받아 `orca skills install`로 Orca에 포함된 에이전트 안내를 추가하고 Orca에 프로젝트를 등록한다. 배정 어댑터는 P3o 레인이 계속 담당한다.<br>• **Aside**(aside.com)는 Claude Code와 Codex가 MCP를 통해 웹 작업에 쓸 수 있는 AI 에이전트가 있는 브라우저다. macOS는 `brew install --cask aside` 또는 공식 DMG, Windows는 공식 설치 프로그램이다. 사용자가 앱 안에서 로그인하고(무료 플랜 있음) 그곳에서 `aside login`을 확인한다. `oah`는 계정을 보지 않는다. `aside` CLI는 제공자가 문서화한 설치 프로그램에서 받으며 실행 전에 출처와 전체 내용을 보여 준다. 동의를 받아 Claude Code(`claude mcp add --scope user --transport stdio aside -- aside mcp`)와 Codex(`codex mcp add aside -- aside mcp`)의 MCP 서버를 등록한다. 둘 다 사용자 수준 설정을 쓰므로 변경을 먼저 보여 준다.<br>• Aside가 등록되면 규칙 블록(5.11)에 선택 절을 추가한다. 웹 작업에 Aside 도구를 쓰고 페이지 내용을 지시가 아닌 데이터로 다루며 결제를 확정하지 않는다.<br>• `oah doctor`는 둘 다 공식 설치 명령과 함께 선택 사항으로 나열하며 없어도 실패하지 않는다. 계정, 자격 증명, 개인 경로는 저장소에 들어가지 않는다(A17). | 5.11, 6.5 | P8(P8c 레인), P7b, P9 |
| A21 | **사용자 프로젝트용 CI 스타터.** `oah ci init`과 동의받은 안내형 `oah init`은 전체 내용을 보여 준 뒤 `.github/workflows/oah-ci.yml`을 쓴다.<br>• 테스트 작업은 사용자가 고른 OS에서 실행한다(기본은 현재 OS). `oah`가 프로젝트에서 찾고 사용자가 확인한 테스트 명령을 쓰거나 채워야 한다고 뚜렷이 표시한 단계를 둔다.<br>• 비밀값 스캔 작업은 모든 push와 pull request에서 버전·SHA-256으로 고정한 gitleaks를 실행하며 스캐너 출력·보고서의 비밀값을 모두 가린다.<br>• 모든 액션은 전체 커밋 SHA로 고정하고 `permissions`는 `contents: read`이며 파일에 비밀값이나 토큰이 없다(A17).<br>• 기존 워크플로는 차이를 보여 주고 묻기 전에는 덮어쓰지 않는다.<br>• 문서는 결과 읽는 법과 GitHub 설정에서 기본 브랜치의 필수 검사로 만드는 법을 설명한다. `oah`가 저장소 설정을 직접 바꾸지는 않는다. | 6.5, 7 | P7(P7c 레인), P7b, P9 |

| A22 | **리뷰는 수렴할 때까지 실행한다.** 고정 라운드 상한이 없다. 매 라운드 지적이 새롭고 범위 안이면 계속한다. 지적의 안정적인 ID와 유형을 저장하며 리뷰어마다 검증한 이전 영수증과 수정 시도 결과를 받는다(5.9). 재발 증거가 없거나 모호한 경우, 수정하려 한 유형을 되풀이하는 지적, 설계·저장 형식 변경이 필요한 지적은 `OAH_REVIEW_NEEDS_DECISION`으로 시리즈를 멈추고 사용자에게 묻는다. 한 라운드가 승인하거나 P2/P3 지적만 보고하면 시리즈를 끝내고 후속 작업으로 기록한다. 예산을 원하는 사용자는 `[review] max_rounds`를 설정한다(기본 미설정). 상한에 닿으면 `OAH_REVIEW_ROUNDS_EXHAUSTED`로 멈추며 `--extra-round`로 계속하고 기록한다. | 4(22행), 5.9 | P6 |

| A23 | **영향은 최신 기본 브랜치를 읽고 증거는 고정한다.** 영향은 최신 기본 브랜치에서 평가한다. 작업은 한 레인씩 그곳에 반영되며 반영 검사는 push나 병합 직전에 fetch한 SHA로 영향을 다시 평가하고 새로 영향받는 검사를 요구한다(5.9). 반영은 리뷰한 정확한 head의 fast-forward push이며 원격 기본 브랜치가 여전히 평가한 SHA와 같다는 조건을 원격이 원자적으로 강제한다. 움직였으면 아무것도 반영하지 않는다. 새 SHA를 fetch하고 영향을 다시 평가하며 영향받는 검사를 다시 요구한 뒤 재시도한다. 반영 영향이 오래됐으면 `OAH_LAND_IMPACT_STALE`, 새로 영향받는 검사가 보류·실패면 `OAH_LAND_CHECKS_REQUIRED`로 거절한다. 질의 전에 `oah impact`는 사용자의 git 설정으로 원격 기본 브랜치를 읽기 전용으로 fetch하고(자격 증명을 읽거나 저장하지 않음, A17) 워크트리 지도 옆에 그 ref의 두 번째 지도를 갱신한다. 출력은 두 지도에 커밋 SHA를 표시한다. 기본적으로 갱신을 켠다(`[impact] refresh_default_branch = true`). 브리프에는 "재정렬기와 `oah impact`의 기본 브랜치 자동 갱신을 제외한 네트워크 금지"라고 적으며 워커가 갱신을 끄게 만드는 표현을 쓰지 않는다. `--offline`이거나 fetch가 실패하면 마지막으로 가져온 ref를 쓰고 `default-branch=stale(<reason>)`으로 표시한다. 리뷰 증거 명령은 리뷰한 커밋과 그 작업 트리에 고정한다. | 5.6, 5.9, 5.10 | P7(P7i 뒤 P7j 레인), P4, P6 |

#### 개정 5 체크리스트

**P8c**
- [ ] 안내형 설정은 Orca와 Aside를 OS별 공식 설치 명령과 함께 선택 사항으로 제안하며 동의 없이는 설치하지 않는다. 관련 없는 `orca` cask가 나타나지 않는지 테스트로 증명한다. (A20)
- [ ] CLI 검사, `orca skills install`, 프로젝트 등록, 두 MCP 등록은 각각 먼저 보여 주고 동의받아야 실행한다. 전역 쓰기는 전역이라고 표시한다. 테스트는 가짜 명령을 쓴다. (A20)
- [ ] Claude MCP 등록 명령에는 `--scope user`가 들어간다. 가짜 명령 테스트로 정확한 인자를 검사한다. (A20)
- [ ] 선택 규칙 절은 Aside가 등록됐을 때만 나타나며 깨끗하게 제거된다. (A20)
- [ ] `oah doctor`는 둘 다 선택 사항으로 보고한다. 없어도 실패하지 않고 계정 데이터를 읽거나 저장하지 않는다. (A20, A17)

**P7c**
- [ ] `oah ci init`은 전체 워크플로를 보여 주고 동의할 때만 쓴다. 기존 워크플로는 차이를 보여 주고 확인받기 전에는 덮어쓰지 않는다. (A21)
- [ ] 워크플로는 모든 액션을 전체 커밋 SHA로, gitleaks를 버전·SHA-256으로 고정하며 `permissions: contents: read`를 쓰고 비밀값이 없다. 테스트는 생성한 텍스트를 검사한다. (A21)
- [ ] 생성한 비밀값 스캔 작업은 스캐너 출력·보고서의 비밀값을 모두 가린다. 실행 중 생성한 비밀값 픽스처로 스캔은 실패하지만 수집한 stdout, stderr, 모든 생성 보고서에는 비밀값이 없는지 확인한다. (A21, A17)
- [ ] 테스트 명령 탐지는 일반적인 Python, Node, Go, Rust 배치를 다루고 사용자 확인을 받으며, 못 찾으면 뚜렷이 표시한 단계로 대체한다. (A21)

**P7b**
- [ ] 안내형 `oah init`이 CI 스타터와 동반 앱을 각각 동의받아 제안한다. (A20, A21)

**P9**
- [ ] 문서는 CI 스타터, CI 결과 읽기, 필수 검사, 동반 앱을 영어와 한국어로 다룬다. (A20, A21)

**P6**
- [ ] 리뷰 시리즈에 고정 상한이 없다. 가짜 리뷰어가 새롭고 범위 안인 지적을 반환하면 세 라운드를 넘어도 계속한다. (A22)
- [ ] 지적은 시리즈 전체의 고유 ID, 안정적인 유형 ID, 실패 원리/규칙 정의를 저장한다. 표현만 바꾼 재발은 같은 유형 ID를 쓴다. (A22)
- [ ] 모든 수정 시도는 원본 영수증 다이제스트, 지적/유형 ID, 전후 head, 증거, 시작, 최종 결과를 저장한다. 무변경, 실패, 중단, 수동 시도도 포함한다. (A22)
- [ ] 리뷰어마다 시리즈의 모든 이전 영수증·시도 기록을 다이제스트와 함께 받는다. 영수증은 전달한 이력의 다이제스트를 기록한다. (A22)
- [ ] 이전 참조는 같은 시리즈의 이전 라운드, 영수증 다이제스트, 지적 ID, 유형 ID를 검증한다. (A22)
- [ ] 끝나지 않은 시도나 깨진 이전 참조를 포함해 재발 이력이 없거나 잘못되거나 모호하면 수렴을 받아들이기 전에 `OAH_REVIEW_NEEDS_DECISION`으로 멈춘다. 첫 라운드는 명시적으로 빈 이력을 받아들인다. (A22)
- [ ] 수정하려 한 유형의 지적은 표현을 바꾸고 새 지적이라고 보고해도 `OAH_REVIEW_NEEDS_DECISION`으로 멈춘다. 시도 없는 이전 지적만으로는 수정 뒤 재발을 증명하지 못한다. (A22)
- [ ] 설계·저장 형식 변경이 필요한 지적은 `OAH_REVIEW_NEEDS_DECISION`으로 멈추고 사용자에게 묻는다. (A22)
- [ ] 승인 라운드나 P2/P3만 있는 라운드는 시리즈를 끝내고 후속 작업을 기록한다. (A22)
- [ ] `[review] max_rounds`를 설정하면 상한에서 `OAH_REVIEW_ROUNDS_EXHAUSTED`로 멈추고 `--extra-round`로 계속하며 기록한다. (A22)

**P7j**(P7i 이후)
- [ ] `oah impact`는 기본적으로 질의 전에 기본 브랜치 지도를 갱신하고 두 지도에 커밋 SHA를 표시한다. (A23)
- [ ] `--offline`이거나 fetch가 실패하면 마지막으로 가져온 ref를 쓰고 `default-branch=stale(<reason>)`으로 표시한다. 테스트는 네트워크 없이 로컬 bare 원격 저장소를 쓴다. (A23)
- [ ] fetch는 자격 증명을 읽거나 git 설정을 바꾸지 않는다. (A23, A17)

**P4**
- [ ] 패킷 템플릿의 "네트워크 금지" 설명은 두 예외를 모두 명시하며 테스트가 표현을 고정한다. (A23)

**P6**
- [ ] 리뷰 증거 명령은 갱신한 기본 브랜치가 아니라 리뷰한 커밋과 작업 트리에서 실행한다. (A23)
- [ ] push나 병합 직전에 반영 검사가 기본 브랜치를 fetch하고 두 지도로 변경 경로의 영향을 다시 평가하며 가져온 SHA, 작업 HEAD, 결과 다이제스트, 필수 검사를 기록한다. (A23)
- [ ] 로컬 bare 원격 저장소에 리뷰 뒤 게이트 결속이 추가되면, 반영 때 가져온 정의로 작업 HEAD에서 새로 영향받는 검사를 요구한다. 보류·실패한 검사는 `OAH_LAND_CHECKS_REQUIRED`로 거절한다. (A23)
- [ ] 앞선 모든 정확한 head 검사가 통과했어도 오프라인 모드, fetch 실패, 반영 영향 확인 불가는 `OAH_LAND_IMPACT_STALE`로 거절한다. (A23)
- [ ] `--merge`와 리드의 push 절차는 실행 직전에 반영 검사를 되풀이한다. 이전에 성공한 `--check`로 이후 기본 브랜치 변경을 건너뛸 수 없다. (A23)
- [ ] 반영은 리뷰한 정확한 head만 fast-forward로 push하며, 기본 브랜치가 평가한 SHA와 같다는 원격의 원자적 조건을 강제한다. (A23)
- [ ] 로컬 bare 원격 저장소에서 검사 뒤 동시 반영으로 기본 브랜치가 앞으로 이동하면 조건부 push는 `OAH_LAND_IMPACT_STALE`로 거절하고 아무것도 반영하지 않는다. 이후 재시도는 새 SHA를 fetch하고 영향을 다시 평가하며 영향받는 검사를 다시 요구한다. (A23)
