# hello-oah: oah로 하는 첫 작업

이 예제는 작은 작업 하나를 `oah`의 전체 흐름에 태워 끝까지 진행한다. Claude Code가 구현하고, Codex가 리뷰하고
고치고, 게이트가 끝났음을 증명하고, 작업이 기본 브랜치에 반영된다. 20분쯤 걸린다.

[English](README.md)

프로젝트는 일부러 아주 작게 만들었다. `hello.py`는 `Hello, world!`를 출력하고 `tests/test_hello.py`가 그것을
검사한다. 이번 작업은 이름을 받아 인사하게 만드는 것이다. `python hello.py Ada`가 `Hello, Ada!`를 출력해야 한다.

이 저장소의 테스트(`tests/test_example_hello.py`)는 실제 Python CHECK 명령을 실행하고 Claude Code, Codex,
unlazy 대역으로 아래의 수정 요청 예시 경로를 검증한다. 오프라인 따라 하기 테스트는 로컬 원격을 쓰고 반영
직전에 멈춘다. GitHub CI나 실제 엔진의 출력은 검증하지 않는다.

## 시작하기 전에

- `oah`가 설치돼 있어야 한다. `oah --version`이 버전을 출력하면 된다. 없으면
  [macOS에 설치하기](../../docs/ko/install-macos.md)나 [Windows에 설치하기](../../docs/ko/install-windows.md)를 따른다.
- Claude Code와 Codex CLI가 설치돼 있고 로그인돼 있어야 한다. `oah doctor`가 둘 다 확인한다.
- 게이트에는 Node.js와 unlazy가 필요하다. unlazy는 `oah init`이 설치를 제안한다.
- 반영 단계에는 gitleaks가 필요하고 GitHub 경로에는 `gh auth login`으로 로그인한 gh도 필요하다. gitleaks가
  없으면 스캔에서 멈추고 gh가 없으면 CI는 미검증으로 남는다.

아래 명령은 모두 터미널에서 실행한다. macOS 명령은 zsh와 bash에서 그대로 된다. Windows PowerShell에서 다르게
써야 하는 줄은 옆에 따로 적었다.

## 1. 내 사본 만들기

이 폴더를 `oah` 체크아웃 밖으로 복사하고 독립된 git 저장소로 만든다.

```sh
cp -R examples/hello-oah ~/hello-oah
cd ~/hello-oah
git init -b main
git add -A
git commit -m "start hello-oah" -m "Agent-Engine: human"
```

Windows PowerShell에서는 앞의 두 줄을 `Copy-Item -Recurse examples\hello-oah $HOME\hello-oah`와
`cd $HOME\hello-oah`로 바꾼다.

반영까지 따라 하려면 GitHub에 빈 저장소를 만들고 초기 브랜치를 올린다. gh에 로그인한 뒤 이 폴더에서
실행한다. 계정에 `hello-oah`가 이미 있으면 다른 저장소 이름을 고른다.

```sh
gh repo create hello-oah --private --source . --remote origin
git push -u origin main
```

대신 8단계까지 로컬에서 연습하려면 내 디스크의 bare 저장소를 쓸 수 있다.

```sh
git init --bare -b main ~/hello-remote.git
git remote add origin ~/hello-remote.git
git push -u origin main
```

Windows PowerShell에서는 `~/hello-remote.git` 대신 `$HOME\hello-remote.git`이라고 쓴다. 원격은 URL이나
절대 경로여야 한다. `../hello-remote.git` 같은 상대 경로는 반영 단계에서 거절된다.
gh가 설치돼 있으면 로컬 대안으로는 9단계를 끝낼 수 없다. gh가 디스크 경로의 GitHub CI를 조회할 수 없어
`OAH_LAND_CI_PENDING`으로 반영이 거절된다. 반영에는 GitHub 경로를 쓴다. gh가 없으면 CI는 `unverified`이고
CI 통과를 증명하지 않는다. gh를 제거하는 것은 CI 검증 방법이 아니다.

## 2. 프로젝트에 oah 설정하기

```sh
oah init
```

`oah init`은 먼저 전체 계획을 출력한 다음 몇 가지를 묻는다. 단계별 에이전트 파일, 훅, unlazy, 지식 그래프,
CI 시작 워크플로, 동반 앱 Orca와 Aside, TypeSafe Jev 재정렬기, 스킬 업데이트다. 이 예제에서는 unlazy만
예라고 답하고 나머지는 아니오라고 답한다. 나머지는 나중에 언제든 더할 수 있다. 사용자가 확인하지 않은
것은 설치하지 않는다. Claude Code와 Codex 로그인은 각 도구의 상태 명령으로 확인한다.

`oah init`은 `.oah/config.toml`, `AGENTS.md`의 규칙 블록(`CLAUDE.md`가 불러온다), `docs/wiki/`의 위키,
`.oah/templates/`의 템플릿, `.gitignore` 항목을 쓰고 첫 영향 지도를 만든다. 마지막 줄은 `OAH_INIT_DONE`이다.
아직 남은 일이 있으면 `OAH_INIT_INCOMPLETE`와 함께 할 일을 알려 준다.

게이트를 실행하기 전에 `.gitignore`에 아래 Python 바이트코드 패턴을 덧붙인다. init은 이 패턴을 더하지 않는다.
단위 테스트 CHECK가 바이트코드를 만들므로 무시되지 않은 새 파일이 생기면 게이트 입력이 달라진다.

```gitignore
__pycache__/
*.py[cod]
```

결과를 커밋한다.

```sh
git add -A
git commit -m "set up oah" -m "Agent-Engine: human"
git push
```

## 3. 작업 적기

브랜치를 만들고 작업 패킷을 만든다. 패킷에는 의도가 들어간다. 문제, 바라는 결과, 성공 조건이다.

```sh
git switch -c hello-name
oah packet new hello-name --scope hello.py --scope tests
```

```text
OAH_PACKET_CREATED .oah/tasks/hello-name/PACKET.md
plan: initial → claude → medium; review → codex → medium; correction → codex → medium
```

plan 줄은 단계마다 맡는 엔진과 tier를 보여 준다. tier는 작업 난이도로 점수를 매겨 정한다.
`.oah/tasks/hello-name/PACKET.md`를 열고 의도를 채운다.

```markdown
Status: accepted
### Problem
`python hello.py` always prints `Hello, world!`; it cannot greet a person by name.
### Desired outcome
`python hello.py Ada` prints `Hello, Ada!`, and without a name it still prints `Hello, world!`.
### Success conditions
- AC1: `python hello.py Ada` prints `Hello, Ada!`.
- AC2: the unit tests pass, including a test for a name.
```

`Status: accepted`는 `Status: draft …` 줄 전체를 바꾼다. 주석 줄과 다른 절은 그대로 둔다. 의도는 한국어로 써도
된다.

이제 게이트를 쓴다. 게이트는 성공 조건마다 그것을 증명하는 검사다.

```sh
oah gates new hello-name
```

`.oah/tasks/hello-name/GATES.md`의 예시를 아래 내용으로 바꾼다.

```markdown
# Gates: hello-name

- [ ] G1: `python hello.py Ada` prints `Hello, Ada!`
  CHECK: python hello.py Ada
  EXPECT: Hello, Ada!
  EVIDENCE: pending

- [ ] G2: the unit tests pass
  CHECK: python -m unittest discover -s tests
  EXPECT: /^OK/m
  EVIDENCE: pending
```

터미널에서 `python`이 Python 3를 실행해야 한다. 그렇지 않으면 Python 3를 실행하는 명령으로 바꿔 쓴다. 두 파일을
검사하고 패킷을 커밋한다.

```sh
oah gates lint hello-name
oah packet check hello-name
git add -A
git commit -m "packet for hello-name" -m "Agent-Engine: human"
```

```text
OAH_PACKET_OK .oah/tasks/hello-name/PACKET.md intent=hello-name@r1 status=accepted acs=2 scope=2 warnings=0
```

`GATES.md`는 git에 들어가지 않는다(`oah init`이 게이트 원장을 무시 목록에 넣는다). 대신 나중에
`oah gates check`가 쓰는 요약 파일을 커밋한다.

## 4. 시작 조건 확인과 리스

```sh
oah preflight --task hello-name
oah start --task hello-name --agent claude --packet .oah/tasks/hello-name/PACKET.md
```

```text
OAH_PREFLIGHT verdict=PROCEED aborts=0 warnings=0
OAH_LEASE_STARTED task=hello-name agent=claude scope=hello.py,tests expires=… intent=hello-name@r1 sha256:…
```

사전 점검은 브랜치가 최신인지, 작업 트리가 깨끗한지, 다른 리스가 이 경로를 잡고 있지 않은지 본다. 리스는
이 작업이 `hello.py`와 `tests`를 쓴다고 선언하고 의도를 거기에 묶는다.

## 5. Claude의 구현

```sh
oah call claude --stage initial --task hello-name
```

이 명령은 패킷으로 만든 지시문을 주고 Claude Code를 대화창 없이 한 번 실행한다. 지시문에는 의도, 범위, 고른
스킬, tier에 맞는 모델과 추론 강도가 들어간다. 이 폴더에서 Claude Code를 직접 열고
`.oah/tasks/hello-name/PACKET.md`의 작업을 하라고 해도 된다. 일하는 방법은 규칙 블록이 알려 준다.

끝나면 `git diff`로 변경을 본다. `greet`가 이름을 받고, `main`이 첫 인자를 넘기고, 테스트에 이름을 확인하는
경우가 들어가 있어야 한다.

## 6. 게이트로 증명하고 커밋하기

```sh
oah gates check hello-name
```

처음 실행하면 CHECK 명령을 보여 주기만 하고 아무것도 실행하지 않는다. 먼저 사용자가 승인해야 한다. 명령을
읽어 본 다음 위키 정리를 먼저 하고 최종 게이트를 실행한다. 위키 정리는 게이트 입력인 추적 파일을 바꾼다.

```sh
oah wiki maintain --task hello-name
oah gates check hello-name --approve
```

```text
OAH_WIKI_MAINTAINED task=hello-name log=added index=added rollover=noop record=…
OAH_GATES_CHECKED task=hello-name run=2 attest=0 carry=0
```

이제 게이트에 증거와 함께 체크 표시가 붙었고 `GATES.summary.md`가 그것을 기록한다. 위키 로그에는 이 작업의
항목이 생겼다. 쓴 엔진을 밝혀 모두 커밋한다.

```sh
git add -A
git commit -m "greet a name" -m "Agent-Engine: claude"
```

## 7. Codex의 리뷰와 수정

```sh
oah review --task hello-name
```

Codex는 커밋된 작업의 깨끗한 사본을 리뷰한다. 실제로 보고한 결과에 따라 진행한다.

- `OAH_REVIEW_CONVERGED`이면 승인됐거나 P2/P3 후속 작업만 남았다. 아래 수정 명령을 건너뛰고 8단계로 간다.
  후속 작업은 나중에 할 일로 기록한다.
- `OAH_REVIEW_CHANGES_REQUESTED`이면 아래 명령으로 보고된 P0/P1 지적을 고친다.
- `OAH_REVIEW_NEEDS_DECISION`이면 진행 방법을 결정한 뒤
  `oah review --task hello-name --decision "<your decision>"`으로 이어 가고 새 결과를 따른다.

아래 빈 이름 지적은 테스트의 가짜 리뷰어가 쓰는 예시다. Claude가 이미 빈 이름을 처리했을 수 있으므로 실제
리뷰에서 이 지적이 나오거나 수정이 필요하리라는 보장은 없다.

```text
OAH_REVIEW_CHANGES_REQUESTED: R1 of task hello-name requested changes (P1×1). Correct them (oah fix --task hello-name runs Codex for that), commit, then run oah review --task hello-name again
R1-F1 P1 quality hello.py:7 — an empty name prints 'Hello, !'
```

이름이 비어 있으면 `Hello, !`가 나온다는 지적이다. 수정 요청이 있을 때만 Codex에게 실제 지적을 고치게 한다.

```sh
oah fix --task hello-name
```

`oah fix`는 Codex가 만든 diff를 출력하고 사용자가 확인할 수 있게 커밋하지 않은 채 남겨 둔다. 여기서는 이름이
비어 있으면 `world`를 쓰도록 `greet`를 고친다. 위키 정리 다음 게이트를 다시 실행하고, 수정을 Codex의 작업으로
커밋하고, 그 커밋을 수정 기록에 묶은 다음 다시 리뷰한다.

```sh
oah wiki maintain --task hello-name
oah gates check hello-name --approve
git add -A
git commit -m "fall back to world for an empty name" -m "Agent-Engine: codex"
oah fix --task hello-name --bind
oah review --task hello-name
```

```text
OAH_FIX_BOUND: the committed correction is bound; run oah review --task <task> for the next round.
OAH_REVIEW_CONVERGED: R2 of task hello-name the series converged at head …
independent: no — codex wrote commits in base..head, so this is not an independent review (an honest label for this engine order, not an error)
```

이 예시의 리뷰는 수렴했다. 라운드 수는 정해져 있지 않다. 새 결과마다 위 규칙을 따른다. 수렴한 뒤 `oah fix`를
실행하면 `OAH_FIX_NOT_REQUESTED`로 거절되므로 8단계로 간다.

## 8. 작업 끝내기

보고(return)는 짧은 결과 보고서다. `INTENT` 한 줄과 성공 조건마다 한 줄씩 적는다. 먼저 `INTENT` 줄을 출력한다.

```sh
oah intent show hello-name
```

프로젝트 폴더 옆에 `~/hello-name-return.txt` 파일을 만들고, 위 명령이 출력한 `INTENT` 줄과 성공 조건마다 한
줄씩 적는다.

```text
INTENT hello-name@r1 sha256:<oah intent show가 출력한 다이제스트>
AC1: met — gate G1
AC2: met — gate G2
```

그리고 작업을 끝낸다.

```sh
oah finish --task hello-name --agent claude --summary "greet a name" --return ../hello-name-return.txt
```

```text
OAH_LEASE_FINISHED task=hello-name agent=claude changed=… gates=met intent=hello-name@r1
```

`finish`는 모든 변경이 범위 안에 있는지, 게이트를 충족했는지, 위키 정리를 했는지, 보고가 의도와 맞는지 확인한
뒤 리스를 닫는다.

## 9. pull request 열고 반영하기

```sh
git push -u origin hello-name
oah review summary --task hello-name
```

요약은 pull request 설명에 붙일 블록이다. 라운드와 판정, 게이트가 들어 있다. gh가 있으면
`gh pr create --fill`로 pull request를 열고 이 블록을 붙여 넣는다.

CI가 돌고 나면 리뷰한 head 그대로 반영할 수 있는지 확인하고 반영한다.

```sh
oah land --check --task hello-name
oah land --merge --task hello-name
```

`oah land --check`는 검사마다 한 줄씩 보여 준다. 리스, 의도, 리뷰, 게이트, 영향, 필수 검사, fast-forward,
스캔, CI다. `--merge`는 모든 검사를 다시 돌리고 `y`를 입력해 확인하라고 묻는다. 확인한 뒤에만 원격의 기본
브랜치를 리뷰한 head로 정확히 fast-forward한다. 그러면 GitHub는 pull request를 병합됨으로 표시한다.

gitleaks가 없으면 스캔 줄이 `OAH_LAND_SCAN_REQUIRED`로 실패하고 아무것도 push하지 않는다. gh가 없으면 CI 줄이
`unverified`라고 나온다. CI를 확인하지 못했다는 뜻이고 `oah`는 이를 통과라고 부르지 않는다.

## 한 일 정리

| 단계 | 명령 | 증명한 것 |
|---|---|---|
| 의도 | `oah packet new`, `oah packet check` | 목표와 성공 조건을 적고 승인했다. |
| 리스 | `oah preflight`, `oah start` | 최신 브랜치에서 시작했고 다른 누구도 이 경로를 잡고 있지 않다. |
| 구현 | `oah call claude --stage initial` | Claude가 패킷을 보고 1차 구현을 했다. |
| 게이트 | `oah gates check --approve` | 성공 조건마다 명령과 기록된 증거가 있다. |
| 리뷰 | `oah review`, `oah fix`, `oah review` | Codex가 리뷰하고 고치고 수렴할 때까지 다시 리뷰했다. |
| 끝내기 | `oah finish` | 변경이 범위 안에 있고 게이트를 충족했고 보고가 의도와 맞는다. |
| 반영 | `oah land --check`, `--merge` | 리뷰한 head 그대로 사용자가 확인한 뒤 반영된다. |

다음으로 각 단계가 왜 있는지는 [개념](../../docs/ko/concepts.md)에서, 명령이 거절할 때는
[문제 해결](../../docs/ko/troubleshooting.md)에서 찾아본다.
