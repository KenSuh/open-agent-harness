# 프로젝트 도구 실행기 번들 진입 스크립트 — PyInstaller 가 얼리는 유일한 시작점으로, 포장된 oah 의 실행기 main 만 부른다.
from oah.project_tools_runner import main

raise SystemExit(main())
