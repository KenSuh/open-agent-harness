// 스킬 출처 수집기 자리: 검토가 출처를 읽는 수집기와, 로컬 수집기가 실행할 호스트 git 을 찾는 연결점이다.
//! Collectors a review reads a skill source with: [`github::GitHub`], which reads a public GitHub repository through
//! the bounded HTTPS client and verifies the tree and every fetched file against their Git ids, and
//! [`local::LocalGit`], the bounded read-only collector of one explicit local fixture repository.

pub mod github;
pub mod local;

use crate::paths::Env;
use crate::proc::resolve::{Resolution, Rule, resolve};
use std::path::PathBuf;

/// The absolute git program [`local::LocalGit`] runs: `git` found on `env`'s PATH by the crate's host resolver.
/// `None` when git is absent or resolves only through a wrapper that is not git itself.
pub fn host_git(env: &Env) -> Option<PathBuf> {
    match resolve(env, "git", Rule::host()) {
        Resolution::Found(found) if found.argv.len() == 1 => Some(PathBuf::from(&found.argv[0])),
        _ => None,
    }
}
