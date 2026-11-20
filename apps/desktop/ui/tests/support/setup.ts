// 한국어: Vitest 공통 설정 — React act 환경 플래그만 켠다.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
