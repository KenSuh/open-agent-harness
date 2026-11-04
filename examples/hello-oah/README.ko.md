# hello-oah: oah로 하는 첫 작업

AI에게 작은 변경을 부탁하고 oah로 어떻게 진행하는지 따라가 본다. Claude Code가 만들고 Codex CLI가 검토하고
고치며 게이트와 위키가 증거를 남긴다. 사용자는 요청하고, AI가 판단을 물으며 멈추면 답하고, 반영 전에
확인한다. 20분쯤 걸린다.

[English](README.md)

프로젝트는 일부러 아주 작게 만들었다. `hello.py`는 `Hello, world!`를 출력하고 `tests/test_hello.py`가 그것을
검사한다. 이번 작업은 이름을 받아 인사하게 만드는 것이다. `python hello.py Ada`가 `Hello, Ada!`를 출력해야 한다.

이 저장소의 테스트(`tests/test_example_hello.py`)는 실제 Python CHECK 명령을 실행하고 Claude Code, Codex,
unlazy 대역으로 아래의 수정 요청 예시 경로를 검증한다. 오프라인 따라 하기 테스트는 로컬 원격을 쓰고 반영
직전에 멈춘다. GitHub CI나 실제 엔진의 출력은 검증하지 않는다.

## 대화로 따라 하기

설치 프로그램을 한 번 실행하고 프로젝트마다 `oah init`을 한 번 실행한다. 그다음 프로젝트에서 AI를 열고 평소
말로 요청한다. 설치된 규칙과 각 명령의 다음 단계 안내가 AI의 하네스 사용을 이끈다.
사용자는 요청하고, AI가 판단을 물으며 멈추면 답하고, 반영 전에 확인한다.

아래 단계는 뒤의 명령 예제와 같은 순서다. 이 예제에서는 먼저 AI에게 연습용 사본을 준비해 달라고 한다.
2단계에서 프로젝트 규칙을 설치한다.

### 1. 연습할 사본을 만든다

- **내가 할 말.** “hello-oah를 별도 프로젝트로 복사해서 연습하게 해 줘.”
- **AI가 하는 일.** 폴더를 복사하고 git과 원격 저장소를 준비한다. 이 단계에는 `oah` 명령이 없다.
- **확인하거나 답할 것.** 로컬 연습용 원격과 GitHub 저장소 중에서 고른다. 로컬 연습은 반영 전에 멈춘다.
  GitHub 경로에는 CI와 아래에 적힌 도구가 필요하다.

### 2. 프로젝트를 한 번 설정한다

- **내가 할 일.** 터미널에서 `oah init`을 실행하고 질문에 답한 뒤 계획을 확인한다. 규칙, 위키, 템플릿이
  설치되고 첫 영향 지도가 만들어진다.
- **AI가 하는 일.** 설정이 끝나면 아래의 바이트코드 무시 항목을 더하고 설정을 커밋한다.
- **확인하거나 답할 것.** 검사에는 unlazy를, GitHub 반영에는 CI 시작 워크플로를 고른다. 전체 설정 계획을
  확인하고 빠진 로그인을 마친다. 이 작은 예제에서는 Jev를 끈다. 키가 없으면 규칙이 난이도와 모델을 정한다.

### 3. 원하는 결과를 말한다

- **내가 할 말.** “hello.py가 이름을 받아 인사하도록 oah로 고쳐 줘. Ada면 ‘Hello, Ada!’가 나오고 이름이
  없으면 ‘Hello, world!’를 유지해 줘.”
- **AI가 하는 일.** `oah packet new`로 작업을 만들고 목표, 범위, 성공 조건을 적는다. `oah gates new`로 검사를
  준비하고 `oah gates lint`와 `oah packet check`로 확인한다.
- **확인하거나 답할 것.** 원하는 동작, 두 검사, `hello.py`와 `tests`라는 범위를 확인한다.
  AI가 선택을 분명히 해 달라고 하면 답한다.

### 4. 시작 조건을 확인한다

- **내가 할 말.** “시작할 준비 됐어?”
- **AI가 하는 일.** `oah preflight`로 시작 조건을 확인하고 영향 지도를 갱신한다. 이어서 `oah start --packet`으로
  작업이 바꿀 경로를 예약한다. 겹치는 리스는 거절한다.
- **확인하거나 답할 것.** 경고나 거절 사유를 읽고, 범위가 겹치면 정리한 뒤 이어 간다.

### 5. 구현을 맡긴다

- **내가 할 말.** “그 범위로 만들어 줘.”
- **AI가 하는 일.** 승인된 작업, 모델, 스킬을 담아 `oah call claude --stage initial`을 실행한다.
  Claude Code가 프로그램을 고치고 테스트를 더한다.
- **확인하거나 답할 것.** 결과와 변경 내역을 본다. 이름을 넣어도 되고, 넣지 않으면 원래 인사말이 나와야 한다.

### 6. 증거를 묻는다

- **내가 할 말.** “게이트 다 통과했어? 위키도 정리했어?”
- **AI가 하는 일.** `oah gates check`로 검사 명령을 보여 주고 `oah wiki maintain`으로 위키를 정리한다.
  승인 뒤 `oah gates check --approve`로 검사하고, 작성한 엔진을 기록해 결과를 커밋한다.
- **확인하거나 답할 것.** 실행 전에 검사 명령을 읽고 승인한다. 끝났다는 말에 그치지 않고 기록된 결과와 위키
  항목을 살핀다.

### 7. 리뷰와 수정을 맡기고 판단에 답한다

- **내가 할 말.** “리뷰 끝났어? 반영을 막는 지적은 고치고 다시 검토해 줘.”
- **AI가 하는 일.** `oah review`를 실행한다. 수정 요청이 있으면 `oah fix`로 고치고 위키 정리, 게이트 재검사,
  확인한 수정의 커밋, `oah fix --bind`, 재검토 순서로 진행한다.
- **확인하거나 답할 것.** 커밋 전에 수정 내역을 살핀다. 같은 문제가 반복되거나 설계 선택이 필요하면 멈추고
  판단을 구한다. 답하면 AI가 `oah review --decision`으로 이어 간다. 수렴해도 후속 작업이 남을 수 있고,
  Codex가 자기 수정을 검토한 경우에는 독립 검토가 아니라고 표시한다.

### 8. 작업을 닫는다

- **내가 할 말.** “목표를 어떻게 충족했는지 보여 주고 작업을 끝내 줘.”
- **AI가 하는 일.** `oah intent show`로 반환 보고를 준비하고 `oah finish`를 실행한다. 범위, 게이트, 위키 정리와
  보고를 확인한 뒤 리스를 닫는다.
- **확인하거나 답할 것.** 성공 조건마다 결과를 보고 남은 후속 작업을 확인한다. 작업을 닫은 뒤 다음 단계에서
  반영한다.

### 9. 반영 전에 확인한다

- **내가 할 말.** “이 버전 반영할 준비 됐어?” 검사를 살핀 뒤 “이 버전을 반영해 줘.”라고 확인한다.
- **AI가 하는 일.** 브랜치를 올리고 `oah review summary`로 pull request를 준비한 뒤 `oah land --check`를
  실행한다. 사용자가 확인하면 `oah land --merge`가 다시 검사하고 검토한 커밋을 반영한다.
- **확인하거나 답할 것.** 정확한 버전, 리뷰, 게이트, 비밀 검사와 CI 결과를 살핀다. 반영할 결과에만 최종 확인을
  주며, 거절 사유가 있으면 해결한다.

**v0.4.0에 계획된 기능이다.** 구현 전에 만들고 싶은 것을 말하면 oah가 추천안과 함께 설계 질문을 하나씩 묻게
된다. 이 안내형 설계 대화는 아직 제공하지 않는다. 지금도 AI와 목표를 의논하고 리뷰가 판단을 구하며 멈추면
답할 수 있다.

## 내부에서 실행하는 명령

사용자가 터미널에서 `oah init`을 실행한 뒤에는 설치된 규칙과 각 명령의 다음 단계 안내에 따라 AI가 아래
명령을 실행한다. 각 결과를 직접 보고 싶으면 이 전체 예제를 손으로 실행해도 된다. 모든 `sh` 블록은 실행
순서대로 있으며, 리뷰 절은 실제 판정에 따라 실행할 명령을 구분한다.
AI가 커밋할 때는 자기 엔진을 기록하며, 아래 예제는 사용자가 손으로 실행하므로 `Agent-Engine: human`을 쓴다.

### 시작하기 전에

- `oah`가 설치돼 있어야 한다. `oah --version`이 버전을 출력하면 된다. 없으면
  [macOS에 설치하기](../../docs/ko/install-macos.md)나 [Windows에 설치하기](../../docs/ko/install-windows.md)를 따른다.
- Claude Code와 Codex CLI가 설치돼 있고 로그인돼 있어야 한다. `oah doctor`가 둘 다 확인한다.
- 게이트에는 Node.js와 unlazy가 필요하다. unlazy는 `oah init`이 설치를 제안한다.
- 반영 단계에는 gitleaks가 필요하고 GitHub 경로에는 `gh auth login`으로 로그인한 gh도 필요하다. gitleaks가
  없으면 스캔에서 멈추고 gh가 없으면 CI는 미검증으로 남아 기본적으로 반영을 막는다.

아래 명령은 모두 터미널에서 실행한다. macOS 명령은 zsh와 bash에서 그대로 된다. Windows PowerShell에서 다르게
써야 하는 줄은 옆에 따로 적었다.

### 1. 내 사본 만들기

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
기본적으로 `OAH_LAND_CI_UNVERIFIED`로 반영을 막는다. gh를 제거하는 것은 CI 검증 방법이 아니다.

### 2. 프로젝트에 oah 설정하기

```sh
oah init
```

`oah init`은 먼저 선택 사항을 묻는다. 단계별 에이전트 파일, 훅, unlazy, 지식 그래프,
CI 시작 워크플로, 동반 앱 Orca와 Aside, TypeSafe Jev 재정렬기, 스킬 업데이트다. 이 예제에서는 unlazy에
예라고 답한다. 9단계에서 GitHub에 반영하려면 CI 시작 워크플로에도 예라고 답하고 Python 테스트 명령을
확인한다. 8단계까지 로컬에서 연습하려면 CI에 아니오라고 답한다. 다른 선택 사항은 아니오라고 답한다.
나중에 언제든 더할 수 있다. 선택을 마친 뒤 전체 계획을 살펴보고 승인해야 변경이 적용된다.
사용자가 확인하지 않은 것은 설치하지 않는다. Claude Code와 Codex 로그인은
각 도구의 상태 명령으로 확인한다.

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

### 3. 작업 적기

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

### 4. 시작 조건 확인과 리스

```sh
oah preflight --task hello-name
oah start --task hello-name --agent claude --packet .oah/tasks/hello-name/PACKET.md
```

```text
OAH_PREFLIGHT verdict=PROCEED aborts=0 warnings=0
OAH_LEASE_STARTED task=hello-name agent=claude scope=hello.py,tests expires=… intent=hello-name@r1 sha256:…
```

사전 점검은 브랜치가 최신인지, 작업 트리가 깨끗한지, 다른 리스가 이 경로를 잡고 있지 않은지 본다. 리스는
이 작업이 바꿀 `hello.py`와 `tests`를 예약하고 의도를 거기에 묶는다. 겹치는 리스는 거절한다.

### 5. Claude의 구현

```sh
oah call claude --stage initial --task hello-name
```

이 명령은 패킷으로 만든 지시문을 주고 Claude Code를 대화창 없이 한 번 실행한다. 지시문에는 의도, 범위, 고른
스킬, tier에 맞는 모델과 추론 강도가 들어간다. 이 폴더에서 Claude Code를 직접 열고
`.oah/tasks/hello-name/PACKET.md`의 작업을 하라고 해도 된다. 일하는 방법은 규칙 블록이 알려 준다.

끝나면 `git diff`로 변경을 본다. `greet`가 이름을 받고, `main`이 첫 인자를 넘기고, 테스트에 이름을 확인하는
경우가 들어가 있어야 한다.

### 6. 게이트로 증명하고 커밋하기

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

### 7. Codex의 리뷰와 수정

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

### 8. 작업 끝내기

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

### 9. pull request 열고 반영하기

```sh
git push -u origin hello-name
oah review summary --task hello-name
```

요약은 pull request 설명에 붙일 블록이다. 라운드와 판정, 게이트가 들어 있다. gh가 있으면
`gh pr create --fill`로 pull request를 열고 이 블록을 붙여 넣는다.

2단계에서 CI 시작 워크플로를 켰다면 리뷰한 head의 CI 결과를 기다리고 확인한 뒤 반영한다. CI를 껐다면 이
새 저장소에는 워크플로가 없다. gh가 있어도 기다리는 것만으로 결과가 생기지 않고
`OAH_LAND_CI_UNVERIFIED`로 반영을 막는다. CI를 검증하려면 작업을 시작하기 전인 2단계로 돌아가 시작
워크플로를 켠다. 리뷰 뒤에 더하면 head가 바뀌므로 게이트와 새 리뷰가 필요하다. 명시적 CI 미검증 예외는
아래에 설명한다.

CI 결과가 있으면 다음을 실행한다.

```sh
oah land --check --task hello-name
oah land --merge --task hello-name
```

`oah land --check`는 검사마다 한 줄씩 보여 준다. 리스, 의도, 리뷰, 게이트, 영향, 필수 검사, fast-forward,
스캔, CI다. `--merge`는 모든 검사를 다시 돌리고 `y`를 입력해 확인하라고 묻는다. 확인한 뒤에만 원격의 기본
브랜치를 리뷰한 head로 정확히 fast-forward한다. 그러면 GitHub는 pull request를 병합됨으로 표시한다.

gitleaks가 없으면 스캔 줄이 `OAH_LAND_SCAN_REQUIRED`로 실패하고 아무것도 push하지 않는다. gh가 없으면 CI 줄이
`unverified`로 남고 `OAH_LAND_CI_UNVERIFIED`로 반영을 막는다. 결과가 없거나 필수 검사 결과가 빠져도 같다.
이 위험을 받아들이려면 위의 두 명령에 `--accept-unverified-ci "<reason>"`을 붙인다. 예외와 사유는 정확히
해당 head에 기록하며 새 head에는 별도 예외가 필요하다. CI는 `unverified`로 남아 통과로 세지 않고, 실패하거나
진행 중인 CI는 여전히 반영을 막는다. 사유에는 비밀을 넣지 않는다. 대상 브랜치의 CI 정책은
[반영](../../docs/ko/concepts.md#반영)을 참고한다.

## 한 일 정리

| 단계 | 명령 | 증명한 것 |
|---|---|---|
| 의도 | `oah packet new`, `oah packet check` | 목표와 성공 조건을 적고 승인했다. |
| 리스 | `oah preflight`, `oah start` | 최신 브랜치에서 시작하고 작업 경로를 예약했다. 겹치는 리스는 거절한다. |
| 구현 | `oah call claude --stage initial` | Claude가 패킷을 보고 1차 구현을 했다. |
| 게이트 | `oah gates check --approve` | 성공 조건마다 명령과 기록된 증거가 있다. |
| 리뷰 | `oah review`, `oah fix`, `oah review` | Codex가 리뷰하고 고치고 수렴할 때까지 다시 리뷰했다. |
| 끝내기 | `oah finish` | 변경이 범위 안에 있고 게이트를 충족했고 보고가 의도와 맞는다. |
| 반영 | `oah land --check`, `--merge` | 리뷰한 head 그대로 사용자가 확인한 뒤 반영된다. |

다음으로 각 단계가 왜 있는지는 [개념](../../docs/ko/concepts.md)에서, 명령이 거절할 때는
[문제 해결](../../docs/ko/troubleshooting.md)에서 찾아본다.
