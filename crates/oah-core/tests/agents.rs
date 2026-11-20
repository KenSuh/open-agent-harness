// 에이전트 시험 묶음: 가짜 확인 통로와 가짜 홈으로 어댑터·세션 작업·DTO 표본을 본다(실제 에이전트는 실행하지 않는다).
#[path = "support/fake_agents.rs"]
mod fake_agents;

#[path = "agents/adapters.rs"]
mod adapters;
#[path = "agents/detection.rs"]
mod detection;
#[path = "agents/dto.rs"]
mod dto;
#[cfg(unix)]
#[path = "agents/home.rs"]
mod home;
#[cfg(unix)]
#[path = "agents/machine.rs"]
mod machine;
#[path = "agents/project.rs"]
mod project;
#[path = "agents/races.rs"]
mod races;
#[path = "agents/session.rs"]
mod session;
