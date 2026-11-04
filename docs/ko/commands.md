# 명령 한눈에 보기

[README](../../README.ko.md) · [동작 방식](how-it-works.md) · [English](../commands.md)

필요한 명령을 찾는 페이지다. [따라 하기 예제](../../examples/hello-oah/README.ko.md)에서 실행 순서를 보여 준다.
패킷은 작업 문서, 리스는 작업할 파일의 임시 담당 표시, 게이트는 증거를 남기는 검사다.
영향 지도는 변경의 영향을 알려 주고, 위키는 결정을 남긴다. tier는 난이도 등급이며 스킬은 AI가 읽는 작업 지침이다.

| 명령 | 하는 일 |
|---|---|
| `oah init` | 안내형 설정. 도구, 로그인, 프로젝트 파일, 첫 영향 지도를 준비한다. |
| `oah doctor` | `oah`가 쓰는 도구를 확인하고 빠진 도구마다 설치 명령을 알려 준다. |
| `oah packet new` / `check` | 작업 패킷(`.oah/tasks/<task>/PACKET.md`)을 만들거나 검사한다. |
| `oah intent show` / `check` | 작업의 의도와 다이제스트, 결속 상태를 보여 준다. |
| `oah preflight` | 시작하기 전에 기본 브랜치, 작업 트리, 리스를 확인한다. |
| `oah start`, `renew`, `status`, `finish`, `release`, `log` | 리스. 경로를 잡고, 연장하고, 목록을 보고, 검사하며 닫고, 내려놓고, 메모를 남긴다. |
| `oah recommend` | 네 가지 난이도 축으로 tier를 고른다. |
| `oah models` | tier마다 쓰는 모델을 보여 주고 `--refresh`로 다시 읽는다. |
| `oah call`, `oah wait` | 단계 하나에 Claude Code나 Codex를 띄우고 실행을 기록한다. 백그라운드 실행을 기다린다. |
| `oah gates` | 작업의 게이트 원장을 쓰고, 검토하고, 검사한다. |
| `oah check` | 게이트 CHECK 줄에 쓰는 운영체제 공통 검사. |
| `oah wiki` | 프로젝트 위키의 링크와 출처, 정리 상태를 관리한다. |
| `oah impact` | 파일이나 데이터베이스 객체가 바뀔 때 다시 확인할 것을 알려 준다. |
| `oah eval` | 정답을 숨긴 실제 사례를 재현하고 변형별 결과를 한계와 함께 보여 준다. |
| `oah graph` | graphify가 있으면 그 지식 그래프를 쓴다. |
| `oah review` | Codex 리뷰 라운드를 하나 돌리고 영수증을 남긴다. `review summary`는 pull request에 붙일 블록을 출력한다. |
| `oah fix` | 최근 리뷰의 지적을 Codex가 고치게 하고, 커밋한 수정을 기록에 묶는다. |
| `oah land` | 리뷰한 head 그대로 반영해도 되는지 확인하거나, 사용자가 확인한 뒤 반영한다. |
| `oah rules` | `AGENTS.md`의 공유 규칙 블록을 최신으로 유지한다. |
| `oah skills` | 함께 싣거나 연결된 스킬을 보여 주고, 설치하고, 업데이트한다. |
| `oah hooks` | 선택형 Claude Code 훅과 git 훅을 설치하거나 지운다. |
| `oah ci` | 내 프로젝트의 GitHub Actions 검사를 설정한다. |

모든 명령은 `--help`를 받는다. 종료 코드는 `0` 완료, `1` 거절, `2` 사용법 오류 또는 `oah preflight`의 `ABORT` 판정,
`69` 환경 문제다. `75`는 `oah wait`의 제한 시간이 끝나도 실행이 진행 중임을 뜻한다. `75`이면
`oah wait <run-id>`를 다시 실행한다. 거절할 때는 언제나 `OAH_*` 코드로 시작하고 다음에 실행할 것을 알려 준다.

선택형 TypeSafe Jev는 허용된 후보 안에서 모델과 추론 강도를 고를 수 있다.
v0.3.0에서는 난이도도 판정할 수 있다.
[Jev](how-it-works.md#선택형-판정기-jev)에서 이 판정들이 어떻게 동작하는지 설명한다.
[작업 흐름](how-it-works.md#작업-흐름)에서 단계별 보장을, [문제 해결](troubleshooting.md)에서 거절 이유를 설명한다.
