// 스킬 폴더 사실: 모은 스킬 파일들의 공개 computedHash 를 계산하는 모듈을 묶는다.
//! Facts about a skill folder. [`hash`] computes the public `computedHash` of a skill's files.

pub mod hash;

pub use hash::computed_hash;
