# 툴킷

툴킷은 `oah` 가 권하는 스킬, 플러그인, MCP 서버, 명령줄 도구의 모음이며, 항목마다 출처와 라이선스를 적는다.
세 가지 형태로 제공한다.

- **번들 스킬**은 `skills/<name>/` 에 들어 있다. 고정한 업스트림 커밋의 파일을 한 바이트도 바꾸지 않고 담으며,
  업스트림 라이선스 파일과 출처 기록 `UPSTREAM.json` 을 함께 둔다. 재배포를 허락하는 라이선스의 스킬만 번들한다.
  **선택 사용** 항목은 `--all` 로 설치되지 않으며, 지정한 작업에만 고른다(예: 한국어 글에만 쓰는 `humanize-korean`).
- **연결 항목**은 공식 설치 명령과 출처만 싣는다. 라이선스가 재배포를 허락하지 않거나, 고치지 않은 파일이
  이 저장소의 개인정보 검사를 통과하지 못하는 경우다.
- **MCP 템플릿**(`mcp/`)은 Claude Code 나 Codex 에서 서버를 띄운다. 값은 절대 넣지 않고 `${NAME}` 자리표시자(Claude)나
  `env_vars` 이름(Codex)만 둔다. 키를 어디에 두는지는 `mcp/README.md` 에 있다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `oah skills list` | 번들·캐시·연결·설치된 스킬을 버전이나 커밋, 출처, 라이선스, 위치와 함께 보여 준다. 읽기만 한다. |
| `oah skills install <name>` | 번들 스킬을 이 프로젝트의 `.claude/skills/`, `.agents/skills/` 에 복사한다(`--target claude\|codex\|both`). 짧은 자체 리스 아래에서 하며 계획을 먼저 보여 준다. |
| `oah skills install <name> --global` | 사용자 폴더 `~/.claude/skills/`, `~/.agents/skills/` 에 복사한다. 전역 파일이라 먼저 묻는다(`--yes` 로 동의). |
| `oah skills install <연결 항목>` | 물어본 뒤 공식 설치 명령을 실행하거나, Claude Code 에 직접 입력할 단계를 알려 준다. |
| `oah skills update` | 키트 캐시를 지금 갱신한다. `--check` 는 보고만 한다. |
| `oah skills update --project` | 이 프로젝트가 추적하는 스킬 사본을 자체 리스 작업으로 갱신한다. |
| `oah skills update --global` | 확인을 받은 뒤 사용자 수준 사본을 갱신한다. |
| `oah skills schedule install\|remove\|status` | 자동 검사를 위한 매일 실행 항목(macOS launchd, Windows 작업 스케줄러)을 동의를 받아 추가하거나 지운다. |

`install` 에 `--dry-run` 을 붙이면 계획만 보여 주고 아무것도 쓰지 않는다. 거절할 때는 다음에 실행할 명령을 함께 알려 준다.

## 스킬을 읽는 순서

같은 이름의 스킬이 여러 곳에 있으면 `oah` 는 처음 찾은 사본을 쓴다.

1. 프로젝트의 `.claude/skills/` 나 `.agents/skills/` 사본.
2. oah 상태 폴더(저장소의 git 폴더 안, 작업 트리 밖)의 키트 캐시.
3. 사용자 폴더 `~/.claude/skills/`, `~/.agents/skills/` 사본, 그다음 플러그인 사본.

## 갱신을 안전하게 하는 방법

- **자동 갱신은 키트 캐시에만 쓴다.** 현재는 변경을 일으키는 `oah skills` 명령이 실행될 때, `[skills] update_interval_days`
  (기본 1일)마다 한 번까지 검사한다. 리스를 가진 작업이 있으면 기다리며, 유휴 확인부터 게시까지 상태 잠금을 쥐고
  있어 갱신 도중에 리스가 시작될 수 없다. `.oah/config.toml` 에 `auto_update = false` 를 두면 끈다.
  `oah start` 등 일반 변경 명령의 연결은 이 레인 병합 뒤 후속 작업이다. 그전에는 변경하는 스킬 명령이나
  `oah skills schedule install` 을 사용한다.
- **프로젝트 사본은 핀이다.** 추적되는 `.claude/skills/`, `.agents/skills/` 는 그 폴더에 자체 리스를 잡는
  `oah skills update --project` 로만 바뀐다.
  재설치는 기존 핀을 보존한다. 캐시가 앞서 있으면 `oah skills update --project` 를 실행한다.
- **전역 사본은 `--global` 과 확인으로만 바뀌며**, 예약 실행으로는 바뀌지 않는다.
  `~/.oah/toolkit/locks/` 의 대상별 잠금이 저장소 간 게시를 직렬화한다. 기준 사본이 바뀌면 갱신을 보류하므로
  갱신 명령을 다시 실행해 현재 사본을 기준으로 준비한다.
- **가져오기는 허용된 환경 변수와 임시 홈만 사용한다.** 사용자 정보가 들어 있는 프록시 URL 은 거부한다.
  자격 증명이 포함되지 않은 프록시를 설정한 뒤 다시 실행한다.
- **항목을 묶어 두려면** `[skills]` 아래에 `pins = ["<name>"]` 를 둔다. unlazy 는 이 장치로 갱신하지 않으며,
  게이트 레인이 시험한 커밋으로 설치한다.
- **모든 갱신은 게시 전에 검사한다.** 라이선스는 SPDX id 와 본문이 같아야 하고, 스킬마다 적재되어야 하며
  (`name`·`description` 문자열이 있는 머리말, 풀리는 링크), 파일은 비밀값 할당·키 모양·개인정보 검사(설치되어 있으면 gitleaks 도)를
  통과해야 한다. 새 실행 파일, 바이너리 파일, 큰 크기 증가는 수상한 변경으로 본다.
  필수 메타데이터는 YAML 1.2 core 규칙에서 문자열이어야 한다. 숫자처럼 보이는 문구는 따옴표로 감싼다.
  Windows 장치 이름과 이식 가능한 경로의 충돌은 파일을 쓰기 전에 거부한다.
- **실패하거나 수상한 갱신은 마지막 정상 사본을 지키고** 이유와 함께 보고한다. 교체가 실패하면 이전 사본을 되돌린다.
  모든 결과는 이전·새 커밋, 시각, 이유와 함께 oah 상태 폴더의 `toolkit/updates.jsonl` 에 남는다.
- **키트 관리자는** 실제 키트 저장소의 로컬 설치에서 `oah skills update --vendored --yes` 를 실행한다.
  `skills/` 와 카탈로그에 자체 리스를 잡으므로 충돌하는 작업을 먼저 끝낸다.

## 번들 스킬 (`skills/` 에 그대로 포함)

| 이름 | 출처 | 고정 커밋 | 라이선스 | 판정 | 설치 |
|---|---|---|---|---|---|
| `computer-use` | <https://github.com/stablyai/orca> `skills/computer-use` | `a0cd1644f320` | MIT | vendor | `oah skills install computer-use` |
| `orca-cli` | <https://github.com/stablyai/orca> `skills/orca-cli` | `a0cd1644f320` | MIT | vendor | `oah skills install orca-cli` |
| `orchestration` | <https://github.com/stablyai/orca> `skills/orchestration` | `a0cd1644f320` | MIT | vendor | `oah skills install orchestration` |
| `typesafe-ai` | <https://github.com/typesafe-ai/skills> `skills/typesafe-ai` | `65a39f393687` | MIT | vendor | `oah skills install typesafe-ai` |
| `skill-creator` | <https://github.com/anthropics/skills> `skills/skill-creator` | `dbd4588f9e10` | Apache-2.0 | vendor | `oah skills install skill-creator` |
| `mattpocock-skills` | <https://github.com/mattpocock/skills> `skills` | `49dd158d1076` | MIT | vendor | `oah skills install mattpocock-skills` |
| `codex-plugin-cc` | <https://github.com/openai/codex-plugin-cc> `plugins/codex` | `db52e28f4d9d` | Apache-2.0 | vendor | `oah skills install codex-plugin-cc` |
| `ponytail` | <https://github.com/DietrichGebert/ponytail> `skills` | `2ed6c52c9d7e` | MIT | vendor | `oah skills install ponytail` |
| `humanize-korean` | <https://github.com/epoko77-ai/im-not-ai> `skills`, `scripts`, `agents`, `.claude-plugin` | `b37e0206d31f` | MIT | vendor, 선택 사용 (다음 작업에만 고름: korean-prose) | `oah skills install humanize-korean` |

## 연결 스킬·플러그인

| 이름 | 출처 | 버전 | 라이선스 | 판정 | 설치 | 연결로 두는 이유 |
|---|---|---|---|---|---|---|
| `unlazy` | <https://github.com/Leonxlnx/unlazy> | 2.1.0 (16671491f667) | MIT | link | oah init --with-unlazy installs unlazy at the commit and scripts digest that lane P5 contract-tests (available once P5 lands); upstream: https://github.com/Leonxlnx/unlazy | 게이트가 쓰는 운영 의존성이라 P5 가 계약 시험한 핀으로만 설치하고 자동 갱신하지 않는다. 저장소의 테스트 파일에 절대 홈 경로가 있어 이 키트의 개인정보 검사도 통과하지 못하며, 번들 파일은 고치지 않는다. |
| `superpowers` | <https://github.com/obra/superpowers> | 7.0.0 (bb92a7774141) | MIT | link | Claude Code 에 입력: `/plugin install superpowers@claude-plugins-official` | 스킬 파일 두 개(토큰 변수 대입, 절대 홈 경로)가 고치지 않은 상태로는 이 키트의 개인정보 검사를 통과하지 못해 연결로 둔다. |
| `graphify` | <https://github.com/Graphify-Labs/graphify> | 0.9.93 (PyPI graphifyy) | Apache-2.0 | link | `pipx install graphifyy`<br>`graphify install` | 업스트림에 스킬 폴더가 없고 CLI 가 템플릿에서 스킬을 써 넣으므로, 고치지 않고 번들할 대상이 없다. |
| `vercel-react-best-practices` | <https://github.com/vercel-labs/agent-skills> | 1.0.0 (063bee94c3f4) | NOASSERTION | link | `npx skills add vercel-labs/agent-skills` | 업스트림이 라이선스를 밝히지 않아 키트가 재배포할 수 없다. |
| `docx` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | Claude Code 에 입력: `/plugin marketplace add anthropics/skills`<br>Claude Code 에 입력: `/plugin install document-skills@anthropic-agent-skills` | 라이선스가 독점이라 키트가 재배포할 수 없다. Claude Code 플러그인 마켓에서 설치한다. |
| `pdf` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | Claude Code 에 입력: `/plugin marketplace add anthropics/skills`<br>Claude Code 에 입력: `/plugin install document-skills@anthropic-agent-skills` | 라이선스가 독점이라 키트가 재배포할 수 없다. Claude Code 플러그인 마켓에서 설치한다. |
| `pptx` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | Claude Code 에 입력: `/plugin marketplace add anthropics/skills`<br>Claude Code 에 입력: `/plugin install document-skills@anthropic-agent-skills` | 라이선스가 독점이라 키트가 재배포할 수 없다. Claude Code 플러그인 마켓에서 설치한다. |
| `xlsx` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | Claude Code 에 입력: `/plugin marketplace add anthropics/skills`<br>Claude Code 에 입력: `/plugin install document-skills@anthropic-agent-skills` | 라이선스가 독점이라 키트가 재배포할 수 없다. Claude Code 플러그인 마켓에서 설치한다. |
| `lazycodex` | <https://github.com/code-yeongyu/oh-my-openagent> | 5.1.40 | LicenseRef-Sustainable-Use-1.0 | link | `npx lazycodex-ai install` | 소스 공개 라이선스(Sustainable Use 1.0)는 오픈소스 라이선스가 아니어서 키트가 재배포하지 않는다. |

## MCP 서버

| 이름 | 출처 | 버전 | 라이선스 | 판정 | 설치 |
|---|---|---|---|---|---|
| `context7` | <https://github.com/upstash/context7> | `@upstash/context7-mcp@4.4.4` | MIT | template | `mcp/claude/context7.json`, `mcp/codex/context7.toml` |
| `korean-law` | <https://github.com/chrisryugj/korean-law-mcp> | `korean-law-mcp@4.15.9` | MIT | template | `mcp/claude/korean-law.json`, `mcp/codex/korean-law.toml` |
| `mcp-openai` | <https://github.com/mzxrai/mcp-openai> | `@mzxrai/mcp-openai@0.1.1` | MIT | template | `mcp/claude/mcp-openai.json`, `mcp/codex/mcp-openai.toml` |
| `aside` | <https://aside.com/> | CLI 1.26.1010.1739 | LicenseRef-Proprietary | link | <https://aside.com/download> 에서 받은 뒤 `aside mcp` 로 등록한다 |
| `openaiDeveloperDocs` | <https://developers.openai.com/mcp> | 호스팅 서비스 | NOASSERTION | link | `codex mcp add openaiDeveloperDocs --url https://developers.openai.com/mcp` |

## 명령줄 도구

| 이름 | 출처 | 버전 | 라이선스 | 판정 | 설치 (macOS · Windows 명령은 `oah doctor` 가 알려 줌) |
|---|---|---|---|---|---|
| `git` | <https://github.com/git/git> | 2.50.1 (Apple Git-155) | GPL-2.0-only | link | xcode-select --install (or: brew install git) |
| `gh` | <https://github.com/cli/cli> | 2.91.0 | MIT | link | brew install gh |
| `node` | <https://github.com/nodejs/node> | v24.6.0 | MIT | link | brew install node |
| `npm` | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link | ships with node |
| `npx` | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link | ships with node |
| `pnpm` | <https://github.com/pnpm/pnpm> | 10.32.1 | MIT | link | npm install -g pnpm (or: corepack enable pnpm) |
| `uv` | <https://github.com/astral-sh/uv> | 0.12.17 | MIT OR Apache-2.0 | link | brew install uv |
| `python3` | <https://github.com/python/cpython> | 3.14.4 | PSF-2.0 | link | brew install python@3.14 |
| `claude` | <https://github.com/anthropics/claude-code> | 2.1.309 | LicenseRef-Anthropic-Commercial | link | npm install -g @anthropic-ai/claude-code |
| `codex` | <https://github.com/openai/codex> | codex-cli 0.163.0 | Apache-2.0 | link | npm install -g @openai/codex (or: brew install --cask codex) |
| `gitleaks` | <https://github.com/gitleaks/gitleaks> | 8.30.1 | MIT | link | brew install gitleaks |
| `graphify` | <https://github.com/Graphify-Labs/graphify> | 0.9.93 (PyPI graphifyy) | Apache-2.0 | link | pipx install graphifyy (or: uv tool install graphifyy) |
| `aside` | <https://aside.com/> | 1.26.1010.1739 | LicenseRef-Proprietary | link | Download from https://aside.com/download |
| `orca` | <https://github.com/stablyai/orca> | 1.4.237 | MIT | link | Download from https://github.com/stablyai/orca/releases (note: the Homebrew cask named 'orca' is a different project) |
| `jq` | <https://github.com/jqlang/jq> | 1.8.1 | MIT | link | brew install jq |
| `rg` | <https://github.com/BurntSushi/ripgrep> | 15.2.0 | Unlicense OR MIT | link | brew install ripgrep |

## 제외 항목

소유자 전용·비공개 스킬, 계정에 묶인 커넥터, 다른 제품 안에 들어 있는 도구는 키트에 넣지 않는다. 개수만 적는다.

| 분류 | 제외 개수 |
|---|---|
| 스킬 | 13 |
| 플러그인 | 4 |
| MCP 서버 | 4 |
| 명령줄 도구 | 0 |
