use super::inventory::{Payload, validate};
use super::native::{Dir, FileAccess};
use super::{
    Acquisition, Engine, Fingerprint, ObjectId, PublishError, PublishStatus, ReviewBinding,
    SkillLeaf,
};
use crate::review::identity::portable_component_key;
use serde::{Deserialize, Serialize};
use std::collections::hash_map::RandomState;
use std::hash::BuildHasher;
use std::io::Read;

type Result<T> = std::result::Result<T, PublishError>;
const RECEIPT_LIMIT: u64 = 16 << 20;

fn below(dir: &Dir, parts: &[&str], create: bool) -> Result<Option<Dir>> {
    let mut parent = dir.clone();
    for part in parts {
        let Some(child) = parent.directory(part, create)? else {
            return Ok(None);
        };
        parent = child;
    }
    Ok(Some(parent))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Destination {
    project: String,
    engine: Engine,
    leaf: String,
    live_parents: Vec<ObjectId>,
    slot_parents: Vec<ObjectId>,
}

struct Scope {
    target: Destination,
    live: Dir,
    slot: Dir,
}

impl Scope {
    fn open(
        root: &Dir,
        state: &Dir,
        engine: Engine,
        leaf: &SkillLeaf,
        create: bool,
    ) -> Result<Option<Self>> {
        let parent = match engine {
            Engine::Claude => ".claude",
            Engine::Codex => ".agents",
        };
        let hash = Fingerprint::of(portable_component_key(&leaf.0).as_bytes()).to_hex();
        let Some(slot) = below(state, &[parent, &hash], create)? else {
            return Ok(None);
        };
        let Some(live) = below(root, &[parent, "skills"], create)? else {
            return Err(PublishError::Changed);
        };
        let target = Destination {
            project: crate::paths::folder_id(
                &crate::paths::canonical_folder(root.path())
                    .map_err(|_| PublishError::UnsafePath)?,
            ),
            engine,
            leaf: leaf.0.clone(),
            live_parents: live.lineage(root)?,
            slot_parents: slot.lineage(root)?,
        };
        Ok(Some(Self { target, live, slot }))
    }

    fn latest(&self) -> Result<Option<Generation>> {
        let mut count = 0u64;
        let mut last = 0;
        for entry in self.slot.names()? {
            let name = entry.to_str().ok_or(PublishError::InvalidReceipt)?;
            if name.starts_with("build-") {
                continue;
            }
            let number = name
                .strip_prefix("generation-")
                .and_then(|n| n.parse::<u64>().ok())
                .filter(|&n| n > 0 && name == generation_name(n))
                .ok_or(PublishError::InvalidReceipt)?;
            count += 1;
            last = last.max(number);
        }
        if count != last {
            return Err(PublishError::InvalidReceipt);
        }
        if last == 0 {
            return Ok(None);
        }
        let dir = self.slot.child(&generation_name(last))?;
        if !dir.contains("receipt.json")? {
            return Err(PublishError::InvalidReceipt);
        }
        let file = dir.regular("receipt.json", FileAccess::Read)?;
        let mut bytes = Vec::new();
        file.take(RECEIPT_LIMIT + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > RECEIPT_LIMIT {
            return Err(PublishError::InvalidReceipt);
        }
        let sealed: Sealed =
            serde_json::from_slice(&bytes).map_err(|_| PublishError::InvalidReceipt)?;
        let record = sealed.record;
        if record.digest()? != sealed.sha256
            || record.version != 1
            || record.number != last
            || record.target != self.target
            || record.generation != *dir.id()
            || !record.next.valid()
            || (last == 1) != record.previous.is_none()
            || record.previous.as_ref().is_some_and(|old| {
                !old.valid()
                    || !old.binding.same_source(&record.next.binding)
                    || old.binding.review_id == record.next.binding.review_id
            })
        {
            return Err(PublishError::InvalidReceipt);
        }
        dir.check()?;
        Ok(Some(Generation { dir, record }))
    }

    fn prepare(
        &self,
        number: u64,
        previous: Option<Acquisition>,
        binding: &ReviewBinding,
        files: &Payload,
    ) -> Result<Generation> {
        let draft = format!("build-{:016x}", RandomState::new().hash_one(number));
        let dir = self.slot.create_dir(&draft)?;
        let next = Acquisition::stage(&dir.create_dir("next")?, binding, files)?;
        let record = Record {
            version: 1,
            number,
            generation: dir.id().clone(),
            target: self.target.clone(),
            previous,
            next,
        };
        let sealed = Sealed {
            sha256: record.digest()?,
            record,
        };
        let bytes = serde_json::to_vec(&sealed).map_err(|_| PublishError::InvalidReceipt)?;
        if bytes.len() as u64 > RECEIPT_LIMIT {
            return Err(PublishError::InvalidReceipt);
        }
        dir.write_new("receipt.json", &bytes)?;
        let id = dir.id().clone();
        drop(dir);
        self.slot
            .move_new(&draft, &self.slot, &generation_name(number), &id)?;
        self.latest()?
            .filter(|generation| generation.record == sealed.record)
            .ok_or(PublishError::InvalidReceipt)
    }

    fn live_tree(&self) -> Result<Option<Dir>> {
        self.live.directory(&self.target.leaf, false)
    }
}

fn generation_name(n: u64) -> String {
    format!("generation-{n}")
}

#[derive(PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u8,
    number: u64,
    target: Destination,
    generation: ObjectId,
    previous: Option<Acquisition>,
    next: Acquisition,
}

impl Record {
    fn digest(&self) -> Result<String> {
        let bytes = serde_json::to_vec(self).map_err(|_| PublishError::InvalidReceipt)?;
        Ok(Fingerprint::of(&bytes).to_hex())
    }

    fn expected(&self) -> Option<&str> {
        self.previous
            .as_ref()
            .map(|old| old.binding.review_id.as_str())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Sealed {
    record: Record,
    sha256: String,
}

struct Generation {
    dir: Dir,
    record: Record,
}

impl Generation {
    fn prune_previous(&self) -> Result<()> {
        let Some(old) = &self.record.previous else {
            return Ok(());
        };
        if let Some(prior) = self.dir.directory("previous", false)? {
            old.verify(&prior)?;
            drop(prior);
            self.dir
                .move_new("previous", &self.dir, "pruning", &old.directory)?;
        }
        if let Some(pruning) = self.dir.directory("pruning", false)? {
            old.prune(&pruning)?;
            drop(pruning);
            self.dir.remove_empty("pruning", &old.directory)?;
        }
        Ok(())
    }

    fn retire(&self, scope: &Scope) -> Result<()> {
        let Some(old) = &self.record.previous else {
            return Ok(());
        };
        if let Some(prior) = self.dir.directory("previous", false)? {
            return old.verify(&prior);
        }
        old.verify(&scope.live_tree()?.ok_or(PublishError::Changed)?)?;
        scope
            .live
            .move_new(&scope.target.leaf, &self.dir, "previous", &old.directory)?;
        let prior = self.dir.child("previous")?;
        if prior.id() != &old.directory {
            return Err(PublishError::MovedUnverified {
                path: prior.path().to_owned(),
            });
        }
        let verified = old.verify(&prior);
        drop(prior);
        if let Err(changed) = verified {
            // A local edit can straddle the first move. Return the intact entry only into a vacant live name.
            self.dir
                .move_new("previous", &scope.live, &scope.target.leaf, &old.directory)
                .map_err(|_| PublishError::MovedUnverified {
                    path: self.dir.path().join("previous"),
                })?;
            return Err(changed);
        }
        Ok(())
    }

    fn finish(&self, scope: &Scope) -> Result<PublishStatus> {
        let next = &self.record.next;
        next.verify(&self.dir.child("next")?)?;
        self.retire(scope)?;
        self.dir
            .move_new("next", &scope.live, &scope.target.leaf, &next.directory)?;
        next.verify(&scope.live.child(&scope.target.leaf)?)?;
        Ok(if self.record.previous.is_some() {
            PublishStatus::Updated
        } else {
            PublishStatus::Installed
        })
    }

    fn recovery(&self, cause: PublishError) -> PublishError {
        if let Some(old) = &self.record.previous
            && let Ok(Some(prior)) = self.dir.directory("previous", false)
            && old.verify(&prior).is_ok()
        {
            return PublishError::Recovery {
                path: prior.path().to_owned(),
                cause: Box::new(cause),
            };
        }
        cause
    }

    fn complete(&self, scope: &Scope) -> Result<PublishStatus> {
        match self.finish(scope) {
            Ok(status) => Ok(status),
            Err(cause) => {
                if matches!(cause, PublishError::MovedUnverified { .. }) {
                    return Err(cause);
                }
                let restored = self.restore_if_vacant(scope);
                Err(self.recovery(restored.err().unwrap_or(cause)))
            }
        }
    }

    fn restore_if_vacant(&self, scope: &Scope) -> Result<()> {
        let Some(old) = &self.record.previous else {
            return Ok(());
        };
        if scope.live.contains(&scope.target.leaf)? {
            return Ok(());
        }
        let Some(prior) = self.dir.directory("previous", false)? else {
            return Ok(());
        };
        old.verify(&prior)?;
        drop(prior);
        self.dir
            .move_new("previous", &scope.live, &scope.target.leaf, &old.directory)?;
        old.verify(&scope.live.child(&scope.target.leaf)?)
    }
}

pub(super) fn installed(
    root: &Dir,
    engine: Engine,
    leaf: &SkillLeaf,
) -> Result<Option<Acquisition>> {
    root.check()?;
    let Some(state) = below(root, &[".oah", "project-skills-v1"], false)? else {
        return Ok(None);
    };
    let Some(scope) = Scope::open(root, &state, engine, leaf, false)? else {
        return Ok(None);
    };
    let Some(gen_record) = scope.latest()? else {
        return Ok(None);
    };
    let pending = gen_record.dir.contains("next")?;
    let result = (|| {
        let live = scope.live_tree()?.ok_or(PublishError::Changed)?;
        let record = &gen_record.record;
        let acquisition = if *live.id() == record.next.directory {
            &record.next
        } else {
            if !pending {
                return Err(PublishError::Changed);
            }
            record
                .previous
                .as_ref()
                .filter(|old| &old.directory == live.id())
                .ok_or(PublishError::Changed)?
        };
        acquisition.verify(&live)?;
        gen_record.dir.check()?;
        Ok(Some(acquisition.clone()))
    })();
    if pending {
        result.map_err(|err| gen_record.recovery(err))
    } else {
        result
    }
}

fn ignore_private_state(state: &Dir) -> Result<()> {
    const RULE: &[u8] = b"*\n";
    if !state.contains(".gitignore")? {
        state.write_new(".gitignore", RULE)?;
    } else {
        let mut bytes = Vec::new();
        state
            .regular(".gitignore", FileAccess::Read)?
            .take(3)
            .read_to_end(&mut bytes)?;
        if bytes != RULE {
            return Err(PublishError::Occupied);
        }
    }
    Ok(())
}

pub(super) fn apply(
    root: &Dir,
    engine: Engine,
    leaf: &SkillLeaf,
    binding: &ReviewBinding,
    files: &Payload,
    expected: Option<&str>,
) -> Result<PublishStatus> {
    validate(binding, files)?;
    root.check()?;
    let create = expected.is_none();
    let state =
        below(root, &[".oah", "project-skills-v1"], create)?.ok_or(PublishError::StaleRevision)?;
    state.with_lock(|| {
        ignore_private_state(&state)?;
        let scope =
            Scope::open(root, &state, engine, leaf, create)?.ok_or(PublishError::StaleRevision)?;
        apply_locked(&scope, binding, files, expected)
    })
}

fn apply_locked(
    scope: &Scope,
    binding: &ReviewBinding,
    files: &Payload,
    expected: Option<&str>,
) -> Result<PublishStatus> {
    let (number, previous) = match scope.latest()? {
        None => {
            if expected.is_some() {
                return Err(PublishError::StaleRevision);
            }
            if scope.live.contains(&scope.target.leaf)? {
                return Err(PublishError::Occupied);
            }
            (1, None)
        }
        Some(generation) => {
            let record = &generation.record;
            if generation.dir.contains("next")? {
                if expected != record.expected() || !record.next.matches(binding, files) {
                    return Err(generation.recovery(PublishError::Pending));
                }
                return generation.complete(scope);
            }
            let live = scope.live_tree()?.ok_or(PublishError::Changed)?;
            record.next.verify(&live)?;
            if let Some(revision) = expected
                && revision != record.next.binding.review_id
                && !(Some(revision) == record.expected() && record.next.matches(binding, files))
            {
                return Err(PublishError::StaleRevision);
            }
            if record.next.matches(binding, files) {
                return Ok(PublishStatus::Unchanged);
            }
            if expected.is_none() {
                return Err(PublishError::Occupied);
            }
            if !record.next.binding.same_source(binding) {
                return Err(PublishError::SourceMismatch);
            }
            if record.next.binding.review_id == binding.review_id {
                return Err(PublishError::InvalidPayload);
            }
            if record.next.same_bytes(files) {
                return Ok(PublishStatus::Unchanged);
            }
            generation.prune_previous()?;
            (
                record
                    .number
                    .checked_add(1)
                    .ok_or(PublishError::InvalidReceipt)?,
                Some(generation.record.next),
            )
        }
    };
    let generation = scope.prepare(number, previous, binding, files)?;
    generation.complete(scope)
}

#[cfg(test)]
mod tests {
    use super::super::tests::{Fixture, binding, files};
    #[cfg(windows)]
    use super::super::tests::{assert_sharing_violation, hold_without_delete_sharing};
    use super::*;
    use std::fs;

    fn scope(f: &Fixture) -> (Dir, Scope) {
        let state = below(&f.root.dir, &[".oah", "project-skills-v1"], false)
            .unwrap()
            .unwrap();
        let scope = Scope::open(&f.root.dir, &state, Engine::Codex, &f.leaf, false)
            .unwrap()
            .unwrap();
        (state, scope)
    }

    #[test]
    fn collision_after_retirement_retains_verified_recovery_and_resumes_after_restart() {
        let f = Fixture::new();
        f.apply(1, None).unwrap();
        let recovered = {
            let (state, scope) = scope(&f);
            state
                .with_lock(|| {
                    let previous = scope.latest()?.unwrap().record.next;
                    let staged = scope.prepare(2, Some(previous), &binding(2), &files(2))?;
                    staged.retire(&scope)?;
                    Ok(staged.dir.path().join("previous"))
                })
                .unwrap()
        };
        assert!(!f.live().exists());
        fs::create_dir(f.live()).unwrap();
        fs::write(f.live().join("keep"), b"intervening directory").unwrap();
        let reopened = super::super::ProjectRoot::open(&f.path).unwrap();
        let retry = || {
            reopened.apply(
                Engine::Codex,
                &f.leaf,
                &binding(2),
                &files(2),
                Some(&binding(1).review_id),
            )
        };
        let PublishError::Recovery { path, cause } = retry().unwrap_err() else {
            panic!("missing recovery")
        };
        assert_eq!(path, recovered);
        assert!(matches!(*cause, PublishError::Occupied));
        assert_eq!(
            fs::read(recovered.join("references/nested/note.txt")).unwrap(),
            [1, 0, 255]
        );
        assert_eq!(
            fs::read(f.live().join("keep")).unwrap(),
            b"intervening directory"
        );
        assert!(f.apply(3, Some(&binding(1).review_id)).is_err());
        fs::rename(f.live(), f.path.join("user-kept")).unwrap();
        assert_eq!(retry().unwrap(), PublishStatus::Updated);
        assert_eq!(
            fs::read(f.live().join("references/nested/note.txt")).unwrap(),
            [2, 0, 255]
        );
        assert_eq!(
            fs::read(f.path.join("user-kept/keep")).unwrap(),
            b"intervening directory"
        );
    }

    #[test]
    fn invalid_or_copied_receipt_preserves_every_live_file() {
        let other = Fixture::new();
        other.apply(1, None).unwrap();
        let other_slot = scope(&other)
            .1
            .latest()
            .unwrap()
            .unwrap()
            .dir
            .path()
            .join("receipt.json");
        for bytes in [b"{".to_vec(), fs::read(other_slot).unwrap()] {
            let f = Fixture::new();
            f.apply(1, None).unwrap();
            let slot = scope(&f)
                .1
                .latest()
                .unwrap()
                .unwrap()
                .dir
                .path()
                .join("receipt.json");
            fs::write(&slot, &bytes).unwrap();
            assert!(matches!(
                f.root.installed(Engine::Codex, &f.leaf),
                Err(PublishError::InvalidReceipt)
            ));
            assert!(matches!(
                f.apply(2, Some(&binding(1).review_id)),
                Err(PublishError::InvalidReceipt)
            ));
            assert_eq!(fs::read(slot).unwrap(), bytes);
            for (path, data) in files(1) {
                assert_eq!(fs::read(f.live().join(path)).unwrap(), data);
            }
        }
    }

    #[test]
    fn a_native_publication_failure_restores_the_old_tree_to_a_vacant_destination() {
        let f = Fixture::new();
        f.apply(1, None).unwrap();
        let old = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
        let next = {
            let (state, scope) = scope(&f);
            state
                .with_lock(|| {
                    Ok(scope
                        .prepare(2, Some(old.clone()), &binding(2), &files(2))?
                        .dir
                        .path()
                        .join("next"))
                })
                .unwrap()
        };
        #[cfg(target_os = "macos")]
        let change_flags = |flag: &str| {
            assert!(
                std::process::Command::new("/usr/bin/chflags")
                    .args([flag])
                    .arg(&next)
                    .status()
                    .unwrap()
                    .success()
            )
        };
        #[cfg(target_os = "macos")]
        change_flags("uchg");
        #[cfg(windows)]
        let hold = hold_without_delete_sharing(&next);
        let failed = f.apply(2, Some(&old.binding.review_id));
        #[cfg(target_os = "macos")]
        change_flags("nouchg");
        #[cfg(windows)]
        drop(hold);
        #[cfg(windows)]
        assert_sharing_violation(failed);
        #[cfg(target_os = "macos")]
        assert!(matches!(failed, Err(PublishError::Io(_))), "{failed:?}");
        assert_eq!(
            f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap(),
            old
        );
        assert!(!next.parent().unwrap().join("previous").exists());
        assert_eq!(
            f.apply(2, Some(&old.binding.review_id)).unwrap(),
            PublishStatus::Updated
        );
    }

    #[test]
    fn successful_updates_retain_one_prior_payload_and_refuse_to_prune_an_edited_archive() {
        let f = Fixture::new();
        f.apply(1, None).unwrap();
        f.apply(2, Some(&binding(1).review_id)).unwrap();
        let (_, scope) = scope(&f);
        let old = scope.slot.path().join("generation-2/previous");
        fs::write(old.join("user-note"), b"keep").unwrap();
        assert!(matches!(
            f.apply(3, Some(&binding(2).review_id)),
            Err(PublishError::Changed)
        ));
        assert_eq!(fs::read(old.join("user-note")).unwrap(), b"keep");
        assert_eq!(
            fs::read(f.live().join("references/nested/note.txt")).unwrap(),
            [2, 0, 255]
        );
        fs::rename(old.join("user-note"), f.path.join("saved")).unwrap();
        for n in 3..=5 {
            assert_eq!(
                f.apply(n, Some(&binding(n - 1).review_id)).unwrap(),
                PublishStatus::Updated
            );
        }
        let backups: Vec<_> = fs::read_dir(scope.slot.path())
            .unwrap()
            .map(|e| e.unwrap().path().join("previous"))
            .filter(|p| p.exists())
            .collect();
        assert_eq!(backups, [scope.slot.path().join("generation-5/previous")]);
        assert_eq!(
            fs::read(backups[0].join("references/nested/note.txt")).unwrap(),
            [4, 0, 255]
        );
    }

    #[test]
    fn project_lock_excludes_other_engine_writers_until_release() {
        let f = Fixture::new();
        f.apply(1, None).unwrap();
        let (state, _) = scope(&f);
        let apply = || {
            f.root
                .apply(Engine::Claude, &f.leaf, &binding(1), &files(1), None)
        };
        state
            .with_lock(|| {
                assert!(matches!(apply(), Err(PublishError::Busy)));
                assert!(!f.path.join(".claude").exists());
                Ok(())
            })
            .unwrap();
        assert_eq!(apply().unwrap(), PublishStatus::Installed);
    }

    #[test]
    fn interrupted_pruning_checks_surviving_objects_before_resuming() {
        let f = Fixture::new();
        f.apply(1, None).unwrap();
        f.apply(2, Some(&binding(1).review_id)).unwrap();
        let (_, scope) = scope(&f);
        let generation = scope.latest().unwrap().unwrap();
        let old = generation.record.previous.unwrap();
        generation
            .dir
            .move_new("previous", &generation.dir, "pruning", &old.directory)
            .unwrap();
        let pruning = generation.dir.path().join("pruning");
        fs::remove_file(pruning.join("references/nested/note.txt")).unwrap();
        fs::rename(pruning.join("SKILL.md"), f.path.join("kept")).unwrap();
        fs::write(pruning.join("SKILL.md"), b"another file").unwrap();
        assert!(f.apply(3, Some(&binding(2).review_id)).is_err());
        assert_eq!(fs::read(pruning.join("SKILL.md")).unwrap(), b"another file");
        fs::rename(pruning.join("SKILL.md"), f.path.join("keep-that-too")).unwrap();
        assert_eq!(
            f.apply(3, Some(&binding(2).review_id)).unwrap(),
            PublishStatus::Updated
        );
        assert!(!pruning.exists());
        assert_eq!(fs::read(f.path.join("kept")).unwrap(), files(1)["SKILL.md"]);
    }
}
