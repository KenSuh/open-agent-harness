# 키는 어디에 두나

`oah` 자체에는 키가 필요 없다. Claude Code와 Codex는 각자 로그인하고, 키를 쓰는 선택 기능은 키가 없으면 꺼진
채로 있다. 이 문서는 키마다 어디에 두는지, 없으면 무엇이 꺼지는지, 그리고 절대 굽히지 않는 규칙 하나를 설명한다.

[English](../keys.md)

## 철칙

API 키, 토큰, 비밀번호, 쿠키, OAuth 시크릿, 계정 식별자는 프로젝트 파일, 커밋, 이슈, 로그, 스크린숏 어디에도
들어가지 않는다. `.oah/config.toml`, `.mcp.json`, `AGENTS.md`, 테스트 고정 데이터도 마찬가지다.

- 키는 실행할 때만 환경 변수나 OS 키체인에서 읽는다.
- 템플릿과 문서에는 `${TYPESAFE_API_KEY}` 같은 자리표시자 이름만 쓰고 값은 쓰지 않는다.
- `oah`는 키를 파일에 붙여 넣으라고 하지 않는다. 키를 쓰거나 출력하거나 지시문, 실행 기록, 오류 메시지에 넣지도
  않는다.

## 어떤 키가 있나

| 키 | 쓰는 곳 | 없으면 |
|---|---|---|
| 없음 | `oah` 자체. 리스, 패킷, 게이트, 영향 지도, 위키, 리뷰, 반영 | 모두 동작한다 |
| Claude Code 로그인 | Claude Code. `oah`는 `claude auth status`로 확인만 하고 읽지 않는다. | `oah call claude`를 실행할 수 없다 |
| Codex 로그인 | Codex CLI. `oah`는 `codex login status`로 확인만 하고 읽지 않는다. | `oah review`와 `oah fix`를 실행할 수 없다 |
| `TYPESAFE_API_KEY` | 선택 기능인 TypeSafe Jev 재정렬기 | `rerank=unused(no key)`. 정해진 규칙만으로 판단한다 |
| `CONTEXT7_API_KEY`, `LAW_OC`, `OPENAI_API_KEY` | 툴킷의 `mcp/` 템플릿으로 띄우는 MCP 서버. 서버를 띄울 때 Claude Code나 Codex가 읽는다 | 그 서버가 뜨지 않거나 선택 키 없이 돈다. [툴킷](toolkit.md) 참고 |

## Claude Code와 Codex는 각자 로그인한다

터미널에서 각 도구의 로그인 명령을 직접 실행한다. 로그인이 빠졌으면 `oah init`과 `oah doctor`가 알려 준다.

- **Claude Code.** `claude auth login`
- **ChatGPT 요금제로 쓰는 Codex.** `codex login`
- **API 키로 쓰는 Codex.** Codex는 키를 표준 입력으로 받으므로 키가 명령줄에 드러나지 않는다.

  ```sh
  printenv OPENAI_API_KEY | codex login --with-api-key
  ```

  Windows PowerShell에서는 `$env:OPENAI_API_KEY | codex login --with-api-key`라고 쓴다.

두 도구 모두 자격 증명을 프로젝트 밖의 자기 설정 폴더에 둔다. `oah`는 상태 명령(`claude auth status`,
`codex login status`)을 실행해 로그인했는지만 읽는다.

## oah가 키를 찾는 곳

키를 찾는 경로는 `oah.secrets` 하나뿐이다. 이름이 `<NAME>`인 키를 이 순서로 찾는다.

1. **환경 변수 `<NAME>`.** 설정돼 있고 비어 있지 않을 때 쓴다.
2. **OS 키체인.** 서비스 `oah`, 계정 `<NAME>`으로 찾는다.
   - macOS는 내장 `security` 도구로 로그인 키체인을 읽는다.
   - Windows는 자격 증명 관리자에서 대상이 `oah:<NAME>`인 일반 자격 증명을 읽는다.
3. **없음.** 그 기능은 꺼진 채로 있고 `unused(no key)`라고 알린다.

### 환경 변수에 두기

macOS(zsh)에서는 프로젝트 밖의 `~/.zshrc`에 적는다.

```sh
export TYPESAFE_API_KEY="<your key>"
```

Windows에서는 사용자 변수로 설정한 뒤 새 터미널을 연다.

```powershell
setx TYPESAFE_API_KEY "<your key>"
```

### OS 키체인에 두기

키체인을 쓰면 셸 설정 파일에도 키가 남지 않는다. 두 명령 모두 값을 따로 물어보므로 셸 기록에도 남지 않는다.

macOS:

```sh
security add-generic-password -s oah -a TYPESAFE_API_KEY -w
```

Windows(명령 프롬프트나 PowerShell):

```powershell
cmdkey /generic:oah:TYPESAFE_API_KEY /user:oah /pass
```

## 키만으로는 기능이 켜지지 않는다

TypeSafe Jev 재정렬기는 **사용자가 켰고 키도 있을 때만** 돈다. 환경 변수에 키가 있다고 켜지지 않는다. 안내형
`oah init`이 한 번 묻는다. 무엇을 보내는지, TypeSafe가 요청마다 요금을 받을 수 있다는 점을 밝히고 답을
`.oah/config.toml`에 적는다.

```toml
[rerank]
enabled = true
provider = "typesafe"
```

그 뒤로 패킷 계획, 지시문, 실행 기록, 영향 결과마다 `rerank=used`나 `rerank=unused(<이유>)`가 붙는다.

| 결과 | 뜻 |
|---|---|
| `rerank=used` | 재정렬기가 돌아 후보 순서를 다시 매겼다. 후보를 더하거나 빼지는 않는다. |
| `rerank=unused(not-enabled)` | `[rerank] enabled`가 `true`가 아니다. 기본값이다. |
| `rerank=unused(no key)` | 켜 두었지만 위 경로에서 키를 찾지 못했다. |
| `rerank=unused(disabled-by-brief)` | 명령에 `--no-rerank`를 줬거나 패킷에서 껐다. |

다른 변수 이름을 쓰려면 `[rerank.typesafe]`에 `key_name`을 적는다.

## 키가 git에 들어가지 않게 막는 장치

- `oah init`은 `.env`, `.env.*`, `*.pem`, `*.key`, `.oah/secrets*`, `.claude/settings.local.json`을
  `.gitignore`에 넣는다.
- 선택형 `pre-push` 훅(`oah hooks install pre-push`)은 push할 커밋만 gitleaks로 검사하고 무언가 나오면
  `OAH_PUSH_SECRETS`로 거절한다.
- `oah land --check`는 반영할 커밋을 검사하고 gitleaks가 없으면 거절한다(`OAH_LAND_SCAN_REQUIRED`).
- CI 시작 워크플로(`oah ci init`)는 push와 pull request마다 gitleaks 작업을 돌린다. [CI 스타터](ci-starter.md)를
  참고한다.

## 키가 새어 나갔다면

1. **먼저 키를 바꾼다.** 발급한 곳에서 새 키로 교체한다. 커밋이나 push, 로그에 한 번 들어간 키는 1분 뒤에
   지워도 이미 노출된 것이다.
2. 그다음 파일과 커밋에서 지우고 정리한 이력을 push한다.
3. 새 키는 위에서 설명한 대로 환경 변수나 키체인에 둔다.
