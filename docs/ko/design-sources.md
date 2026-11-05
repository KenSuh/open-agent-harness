# 설계 출처

설계의 **재료**는 그 설계가 기대는 라이브러리, 서비스, 문서다. `oah sources` 는 재료가 오는 공개 업스트림을
따라가며, 본 버전마다 검증한 스냅샷을 남기고, 설계의 결정 하나하나가 어느 스냅샷을 보고 내린 것인지
설계 안에 적게 해 준다. 그러면 나중 검사에서 업스트림이 새 버전으로 넘어갔는지, 결정 밑의 요구 사항이
바뀌었는지, 두 스냅샷 사이에 무엇이 달라졌는지를 알 수 있다.

출처 정보는 이 클론의 oah 상태 폴더(저장소의 git 폴더 안, 또는 `OAH_STATE_DIR` 아래)에 있고 커밋되지 않는다.
설계 자체는 `oah design save` 로만 바뀐다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `oah sources register <url>` | GitHub 저장소(`--track release` 또는 `commit`)나 https 문서 페이지(`--track doc`)를 따라간다. `--id`, `--tag-prefix`, `--prerelease`, `--release-order`, `--ref`, `--paths`, `--manifest` 로 볼 범위를 좁히고, `--for-design PROJECT/DESIGN` 으로 설계와 잇는다. |
| `oah sources remove <id>` | 출처 추적을 멈춘다. 저장한 스냅샷과 관측 기록은 남는다. |
| `oah sources list` / `show <id>` | 출처마다 신선도, 마지막 시도, 마지막 확인, 검증한 스냅샷을 보여 준다. 읽기만 한다. |
| `oah sources check [<id>...]` | 지정한 출처, 또는 전체를 지금 검사한다. |
| `oah sources check --scheduled` | 영속 대기열을 한정된 시간 안에 공정하게 한 번 도는 검사다. 매일 OS 항목이 실행하는 것이 이것이다. |
| `oah sources diff <id> --from S1 --to S2` | 한 출처의 저장된 두 스냅샷을 네트워크 없이 비교한다. |
| `oah sources proposals [--design PROJECT/DESIGN]` | 설계가 검토할 만한 출처 변화를 보여 준다. 읽기만 한다. |
| `oah sources dismiss <key> --reason "..."` | 제안 하나를 이유와 함께 숨긴다. |
| `oah sources materials render --design PROJECT/DESIGN --input FILE` | 선택 파일로 설계의 materials 부분을 만든다. 출력하거나 `--out` 으로 새 파일 하나를 쓴다. |
| `oah sources materials check --design PROJECT/DESIGN` | 재료 결정마다 입력과 아직 맞는지 알려 준다. `--revision N` 은 이전 리비전을 검사한다. |
| `oah sources schedule install\|remove\|status` | 매일 실행 항목(macOS launchd, Windows 작업 스케줄러)을 동의를 받아 추가하거나 지우거나 점검한다. |

읽는 명령은 모두 `--json` 으로 전체 보고를 출력한다. 종료 코드는 성공이면 `0`, 거절·부분 실행·검증하지 못한
출처·검토가 필요한 결정이면 `1`, 상태·설정·OS 스케줄러를 쓸 수 없으면 `69` 다(예를 들어
`OAH_SOURCES_STATE_CORRUPT`, `OAH_SCHEDULE_TOOL_MISSING`, `OAH_SCHEDULE_UNSUPPORTED`).

## 검사가 남기는 것

- **스냅샷은 바뀌지 않는다.** 스냅샷은 가져온 내용 그대로의 다이제스트로 구별한다. oah 는 스냅샷을 고쳐 쓰거나
  정리해 지우지 않으므로, 오래된 결정도 그때 기댄 내용과 언제든 비교할 수 있다.
- **시각은 따로 남긴다.** 출처마다 마지막 *시도*, 관측의 마지막 *확인*, 스냅샷의 *검증* 시각을 따로 둔다.
  시도가 실패해도 나머지 두 시각은 움직이지 않는다.
- **신선도**는 `fresh`, `stale`, `unverified`, `unavailable`, `unknown` 중 하나다. 마지막 확인 뒤
  `[sources] fresh_hours` 동안 `fresh` 다.
- **라이선스는 전문을 읽는다.** 실제 라이선스 본문과 하위 폴더의 라이선스 파일까지 남기고,
  `[sources] license_allow` 에 비춰 사용 가능 여부를 판단한다. 알아보지 못한 라이선스는 추측하지 않고 `unknown` 으로 둔다.
- **요청은 공개·무자격 증명이다.** 쿠키나 인증 헤더를 보내지 않으며, HTTP 프록시를 거쳐야 하면 실행하지 않는다.
  프록시가 목적지를 정하면 oah 가 그 목적지를 확인할 수 없기 때문이다.

검사는 출처마다 결과 하나를 알린다. `unchanged`, `recovered`, `changed`, `unverified`, `blocked`, `failed`,
`superseded`, `skipped-fresh`, `skipped-rate-limited`, `over-budget`, `disabled`, `state-corrupt` 중 하나다.

## 두 스냅샷 비교

`oah sources diff` 는 저장된 스냅샷만 읽는다. 네트워크에 접속하지 않고, 확인 시각을 바꾸지 않으며, 설계를 쓰지
않는다. 이 클론에 두 스냅샷 중 하나가 없으면 보고는 `unavailable` 이 되고 없는 스냅샷을 밝힌다.

보고는 `complete`, `partial`, `unavailable` 중 무엇인지 밝힌다. 바뀐 경로를 모두 싣고(텍스트 보기는 처음 200개,
`--json` 은 전부), 라이선스와 릴리스 변화, 그리고 diff 하나에 200줄 이하·모두 합쳐 64 KiB 이하로 텍스트 diff 를
50개까지 보여 준다. 업스트림에서 온 글은 외부 자료로 표시하고, 그 안의 제어·서식 문자는 `‮` 처럼 이스케이프해
보여 주므로 터미널 화면을 바꿀 수 없다. `OAH_SOURCES_SOURCE_MISMATCH` 는 그 ID 가 이제 스냅샷을 가져온 곳과 다른
업스트림을 가리킨다는 뜻이다.

## 설계 안의 재료

`oah sources materials render` 는 선택 파일을 설계의 `materials` 부분으로 바꾼다. 결정은 사용자가 적고, 사실은
모두 oah 가 검증한 스냅샷에서 채운다.

```json
{
  "lang": "ko",
  "decision_inputs": {"calendar": {"need": "강사의 빈 시간을 보여 준다", "platform": ["web"]}},
  "decisions": [{"id": "calendar", "status": "selected", "chosen": "calendar-lib",
                 "reason": "유지 보수 중이고 MIT 라이선스"}],
  "materials": [{"id": "calendar-lib", "source": "calendar-lib", "choice": "selected", "decision": "calendar"}]
}
```

- 라이선스, 릴리스, 스냅샷, 검사 시각은 저장된 스냅샷에서 온다. 파일에 이런 값을 적으면 oah 는 무시하고
  그 사실을 알린다(`OAH_MATERIALS_INPUT_IGNORED`).
- 아직 검증한 스냅샷이 없는 출처는 거절한다. `--allow-unverified` 를 주면 미검증 관측으로 기록한다.
- 이 부분에는 **휴대용 증거**, 곧 재료마다 기댄 바이트 그대로가 들어간다. 출처 상태가 없는 새 클론에서도 검사할
  수 있고, 처음 관측한 시각은 나중에 갱신해도 그대로 남는다. 부분 전체는 1 MiB 미만이어야 하며, 넘으면 아무것도
  쓰기 전에 `OAH_MATERIALS_EVIDENCE_TOO_LARGE` 로 거절한다. 선택 경로를 좁히거나 결정을 여러 설계로 나눈다.
- 결과는 표준 출력으로 나온다. `--out FILE` 은 이 저장소 안에 새 파일 하나만 만들며, 이미 있는 파일, 링크,
  저장소 밖 경로는 거절한다(`OAH_MATERIALS_OUTPUT_EXISTS`, `OAH_MATERIALS_OUTPUT_PATH_INVALID`).
  Windows 에서는 `--out` 을 `OAH_MATERIALS_OUTPUT_UNVERIFIABLE` 로 거절하므로 표준 출력을 리디렉션한다.

결과는
`oah design save <project> <design> --expect-revision N --expect-digest D --reason "..." --part materials=FILE`
로 새 리비전에 저장한다.

`oah sources materials check` 는 결정마다 상태 하나를 준다. 심각한 것부터 적으면 다음 표와 같다.

| 상태 | 뜻 |
|---|---|
| `invalid` | 부분을 손으로 고쳤거나, 증거가 검증되지 않거나, 다른 설계나 알 수 없는 리비전을 가리킨다. |
| `unknown` | 그 부분이 속한 설계를 읽을 수 없다. |
| `stale` | 결정 밑의 승인된 요구 사항이 바뀌었다. |
| `needs-review` | 작업 중인(아직 승인 전) 요구 사항이 바뀌었거나, 출처가 새 스냅샷이나 등록으로 넘어갔거나, 라이선스 정책이 바뀌었고 이를 다루는 해소 기록이 없다. |
| `current` | 결정이 내려질 때의 입력과 아직 맞는다. |

결정의 입력이 바뀌면 선택 파일에 그 결정의 해소 기록을 적기 전까지 render 가 `OAH_MATERIALS_REVIEW_REQUIRED` 로
거절한다. `carry` 는 새 입력에도 같은 재료를 유지하고, `reselected` 는 새로 고른 재료를 기록한다. 해소 기록마다
옛 입력과 새 입력을 정확히 적으므로, 나중 검사에서 손으로 고친 것과 구별된다.

## 매일 검사

`oah sources schedule install` 은 확인을 받은 뒤(`--yes` 로 동의) 이 프로젝트에서
`oah sources check --scheduled` 를 실행하는 매일 OS 항목을 추가한다. 한 번의 실행은 30분 안에서 영속적이고
공정한 대기열을 처리한다. 시간이 다 된 실행은 대기열을 그대로 두어 다음 실행이 처음부터가 아니라 멈춘 곳부터
이어 가고, 앞쪽에서 시간 초과가 이어져도 뒤쪽 출처가 시도되지 못하는 일은 없다. `complete` 는 이번 주기에 모든
출처를 한 번씩 시도했다는 뜻이지, 모든 검사가 성공했다는 뜻이 아니다. 실행 결과는 `complete`,
`partial(over-budget)`, `partial(rate-limited)`, `partial(interrupted)`, `not-due`, `busy`, `disabled` 중
하나이고, 부분 실행이면 `1` 로 끝난다.

OS 는 하루에 한 번 항목을 깨우고, 실제로 검사할 때가 됐는지는 `[sources] check_interval_hours` 가 정한다.
`auto_check = false` 이면 예약 실행은 아무것도 검사하지 않고 그 사실만 기록한다.

`oah sources schedule status` 는 oah 가 설치할 정의와 OS 가 보고하는 정의를 비교해 다른 점을 하나하나 밝힌다.
상태는 `scheduled`, `scheduled-different`, `disabled`, `unverifiable`, `file-only`, `absent`, `unsupported`,
`tool-missing` 중 하나다. 그다음 세 가지 증거를 따로 보여 준다.

- 종류를 가리지 않은 마지막 `oah sources check`. 누가 실행했는지는 알 수 없다.
- 마지막 `--scheduled` 실행. 손으로 실행한 명령도 이것을 남길 수 있다.
- OS 스케줄러가 항목을 실행했다는 자체 기록. OS 가 알려 주지 않으면 `unknown` 이다.

OS 가 실제로 실행했다는 것을 보여 주는 것은 세 번째뿐이다.

## 선택 사항인 판정

oah 는 판정기(TypeSafe Jev, [키](keys.md) 참고)에게 출처 변화가 설계에 중요한지 물을 수 있다. 평소처럼
`[rerank] enabled = true` 와 키가 있어야 하고, 기본값이 꺼짐인 자체 스위치도 켜야 한다.

```toml
[rerank.judgement]
enabled = false           # true 이면 공개 출처 자료로 판정을 허락한다
send_design_text = false  # true 이면 설계 글의 발췌도 보낼 수 있다
```

켜더라도 oah 는 검증한 공개 스냅샷의 조각과 정해진 라벨만 보내고, 설계 발췌는 `send_design_text = true` 일 때만
보낸다. 나가는 문자열은 모두 비밀 값과 이메일 주소를 검사하며, 하나라도 걸리면 아무것도 보내지 않는다. 저장된
판정은 스냅샷, 설계 리비전, 라이선스 정책, 제공자, 모델이 모두 그대로일 때만 적용한다. 아직 판정을 요청하는
`oah sources` 명령은 없으며, 이 설정은 앞으로 판정을 요청할 호출자를 막는 문이다.

판정은 답을 억지로 고르게 하지 않는다. 선택지가 여럿인 질문은 맞는 선택지가 있는지도 함께 물으므로 결과는
`chosen`, `no-match`, `uncertain` 중 하나다. 재료 분류는 하나씩 따로 물으므로 여러 분류가 함께 맞을 수도, 하나도
맞지 않을 수도 있다. 예/아니오 답이 0.7 이상이면 예, 0.3 이하이면 아니오로 읽는다. 이 값은 권장 출발점이며 실제
사례로 맞춘 값이 아니다. 판정은 조언일 뿐이며 무엇도 승인·설치·제거하지 않는다.

## 설정

키는 모두 `.oah/config.toml` 의 `[sources]` 아래에 둔다. 잘못된 키는 `OAH_CONFIG_INVALID` 가 이름을 밝힌다.

| 키 | 기본값 | 뜻 |
|---|---|---|
| `auto_check` | `true` | 예약 실행이 검사를 할지 여부. |
| `check_interval_hours` | `24` | 완료된 실행 뒤 다음 실행이 필요해지기까지의 시간(1~720). |
| `fresh_hours` | `24` | 확인 한 번으로 출처가 `fresh` 로 남는 시간(1~720). |
| `license_allow` | `["MIT"]` | 재료가 쓸 수 있는 라이선스. `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC` 중에서 고른다. |
| `max_document_bytes` | `1048576` | 가져올 문서 페이지의 최대 크기(1024~8388608). |
| `timeout_seconds` | `30` | 요청 하나에 허락하는 시간(1~600). |

## 한계

- GitHub 의 비인증 요청 한도가 적용된다. 한도에 걸린 출처는 나중 실행을 기다린다.
- 스냅샷을 정리해 지우지 않으므로, 상태 폴더는 바뀐 스냅샷마다 커진다.
- 릴리스 선택은 한정된 수의 태그와 릴리스만 읽는다. 전부 볼 수 없으면 최신이라고 주장하지 않고
  `selection-incomplete` 라고 밝힌다. 고른 릴리스는 oah 가 관측한 것 중 최신이며, 더 새 것이 없다는 보장은 아니다.
- Windows 의 `--out` 은 위에서 말한 대로 거절한다.
- `schedule status` 는 macOS 에서 `launchctl print`, Windows 에서 작업 스케줄러의 출력을 읽는다. 읽지 못한 사실이
  있으면 상태를 `unverifiable` 로 두고, 추정하지 않는다.
