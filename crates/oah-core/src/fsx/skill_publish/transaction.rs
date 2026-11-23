use super::inventory::{Payload, validate};
use super::native::{Dir, FileAccess};
use super::{
    Acquisition, Engine, Fingerprint, ObjectId, PublishError, PublishStatus, ReviewBinding,
    SkillLeaf, os,
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
            if name.strip_prefix("build-").is_some_and(|id| {
                id.len() == 16
                    && id
                        .bytes()
                        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            }) {
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
        os::plain_payload(&file)?;
        let mut bytes = Vec::new();
        file.take(RECEIPT_LIMIT + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > RECEIPT_LIMIT {
            return Err(PublishError::InvalidReceipt);
        }
        let sealed: Sealed =
            serde_json::from_slice(&bytes).map_err(|_| PublishError::InvalidReceipt)?;
        let record = sealed.record;
        let valid_version = match record.version {
            1 => (last == 1) == record.previous.is_none(),
            2 => last != 1 || record.previous.is_none(),
            _ => false,
        };
        if record.digest()? != sealed.sha256
            || !valid_version
            || record.number != last
            || record.target != self.target
            || record.generation != *dir.id()
            || !record.next.valid()
            || record.previous.as_ref().is_some_and(|old| {
                !old.valid()
                    || !old.binding.same_source(&record.next.binding)
                    || old.binding.review_id == record.next.binding.review_id
            })
        {
            return Err(PublishError::InvalidReceipt);
        }
        let marker = format!("published-{}", sealed.sha256);
        let phase = Phase::read(&dir, &record, &marker)?;
        dir.check()?;
        Ok(Some(Generation {
            dir,
            record,
            marker,
            phase,
        }))
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
            version: 2,
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

    fn require_vacancy(&self, expected: Option<&str>) -> Result<()> {
        if expected.is_some() {
            return Err(PublishError::StaleRevision);
        }
        if self.live.contains(&self.target.leaf)? {
            return Err(PublishError::Occupied);
        }
        Ok(())
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
    marker: String,
    phase: Phase,
}

#[derive(Clone, Copy)]
enum Backup {
    Previous,
    Pruning,
}

enum Phase {
    Pending {
        parked: bool,
    },
    Unstaged {
        published: bool,
        backup: Option<Backup>,
    },
}

impl Phase {
    fn read(dir: &Dir, record: &Record, marker: &str) -> Result<Self> {
        os::plain_payload(dir.file())?;
        let (mut next, mut published) = (false, false);
        let mut backup = None;
        for name in dir.names()? {
            match name.to_str() {
                Some("receipt.json") => {}
                Some("next") => next = true,
                Some("previous") if backup.is_none() => backup = Some(Backup::Previous),
                Some("pruning") if backup.is_none() => backup = Some(Backup::Pruning),
                Some(name) if name == marker => {
                    let file = dir.regular(marker, FileAccess::Read)?;
                    if file.metadata()?.len() != 0 {
                        return Err(PublishError::InvalidReceipt);
                    }
                    os::plain_payload(&file)?;
                    published = true;
                }
                _ => return Err(PublishError::InvalidReceipt),
            }
        }
        if backup.is_some() && record.previous.is_none() {
            return Err(PublishError::InvalidReceipt);
        }
        match (next, published, backup) {
            (true, false, backup @ (None | Some(Backup::Previous))) => Ok(Self::Pending {
                parked: backup.is_some(),
            }),
            (false, published, backup) => Ok(Self::Unstaged { published, backup }),
            (true, _, _) => Err(PublishError::InvalidReceipt),
        }
    }
}

impl Generation {
    fn mark_published(&self) -> Result<()> {
        self.dir.write_new(&self.marker, &[])?;
        Ok(())
    }

    fn verify_vacant_history(&self, published: bool, backup: Option<Backup>) -> Result<()> {
        if !published {
            return Err(self.recovery(PublishError::Changed));
        }
        match backup {
            Some(Backup::Previous) => self
                .record
                .previous
                .as_ref()
                .ok_or(PublishError::InvalidReceipt)?
                .verify(&self.dir.child("previous")?),
            Some(Backup::Pruning) => Err(PublishError::Pending),
            None => Ok(()),
        }
    }

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
        let live = scope.live.child(&scope.target.leaf)?;
        next.verify(&live)?;
        self.mark_published()?;
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
    let pending = matches!(gen_record.phase, Phase::Pending { .. });
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
    let Some(generation) = scope.latest()? else {
        scope.require_vacancy(expected)?;
        return scope.prepare(1, None, binding, files)?.complete(scope);
    };
    let record = &generation.record;
    let number = record.number;
    let exact = record.next.matches(binding, files);
    let review_mismatch = !exact && record.next.binding.review_id == binding.review_id;
    let previous = match generation.phase {
        Phase::Pending { parked } => {
            if exact && expected == record.expected() {
                return generation.complete(scope);
            }
            if parked {
                return Err(generation.recovery(PublishError::Pending));
            }
            if review_mismatch {
                return Err(PublishError::InvalidPayload);
            }
            record.next.verify(&generation.dir.child("next")?)?;
            match generation.record.previous {
                Some(old) => match scope.live_tree()? {
                    Some(live) => {
                        old.verify(&live)?;
                        authorize_update(&old, binding, expected)?;
                        if old.same_bytes(files) {
                            return Ok(PublishStatus::Unchanged);
                        }
                        Some(old)
                    }
                    None if expected.is_some() => return Err(PublishError::Changed),
                    None => {
                        scope.require_vacancy(None)?;
                        None
                    }
                },
                None => {
                    scope.require_vacancy(expected)?;
                    None
                }
            }
        }
        Phase::Unstaged { published, backup } => {
            if let Some(live) = scope.live_tree()? {
                record.next.verify(&live)?;
                if exact {
                    if expected.is_some()
                        && expected != Some(record.next.binding.review_id.as_str())
                        && expected != record.expected()
                    {
                        return Err(PublishError::StaleRevision);
                    }
                } else {
                    authorize_update(&record.next, binding, expected)?;
                }
                if !published {
                    generation.mark_published()?;
                }
                if exact || record.next.same_bytes(files) {
                    return Ok(PublishStatus::Unchanged);
                }
                drop(live);
                generation.prune_previous()?;
                Some(generation.record.next)
            } else {
                if expected.is_some() {
                    return Err(PublishError::Changed);
                }
                generation.verify_vacant_history(published, backup)?;
                if review_mismatch {
                    return Err(PublishError::InvalidPayload);
                }
                None
            }
        }
    };
    let number = number.checked_add(1).ok_or(PublishError::InvalidReceipt)?;
    let generation = scope.prepare(number, previous, binding, files)?;
    generation.complete(scope)
}

fn authorize_update(
    acquisition: &Acquisition,
    binding: &ReviewBinding,
    expected: Option<&str>,
) -> Result<()> {
    let revision = expected.ok_or(PublishError::Occupied)?;
    if revision != acquisition.binding.review_id {
        return Err(PublishError::StaleRevision);
    }
    if !acquisition.binding.same_source(binding) {
        return Err(PublishError::SourceMismatch);
    }
    if revision == binding.review_id {
        return Err(PublishError::InvalidPayload);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::super::tests::{Fixture, binding, files};
    #[cfg(windows)]
    use super::super::tests::{assert_sharing_violation, hold_without_delete_sharing};
    use super::*;
    use std::fs;

    fn scope(f: &Fixture) -> (Dir, Scope) {
        let root = f.root.project.pin().unwrap();
        let state = below(&root, &[".oah", "project-skills-v1"], false)
            .unwrap()
            .unwrap();
        let scope = Scope::open(&root, &state, Engine::Codex, &f.leaf, false)
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
        assert!(matches!(
            f.apply(3, Some(&binding(1).review_id)),
            Err(PublishError::Recovery { .. })
        ));
        assert!(matches!(
            f.apply(3, None),
            Err(PublishError::Recovery { .. })
        ));
        fs::create_dir(f.live()).unwrap();
        fs::write(f.live().join("keep"), b"intervening directory").unwrap();
        let reopened = super::super::ProjectRoot::open(&f.path, &f.home).unwrap();
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

    fn rolled_back_update() -> (Fixture, Acquisition) {
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
        (f, old)
    }

    #[test]
    fn a_native_publication_failure_restores_the_old_tree_to_a_vacant_destination() {
        let (f, old) = rolled_back_update();
        assert_eq!(
            f.apply(2, Some(&old.binding.review_id)).unwrap(),
            PublishStatus::Updated
        );
    }

    #[test]
    fn a_different_confirmed_update_can_follow_a_verified_rollback() {
        let (f, old) = rolled_back_update();
        assert!(matches!(
            f.apply(3, Some(&binding(9).review_id)),
            Err(PublishError::StaleRevision)
        ));
        let mut wrong_source = binding(3);
        wrong_source.repo = "fixture/other".into();
        assert!(matches!(
            f.root.apply(
                Engine::Codex,
                &f.leaf,
                &wrong_source,
                &files(3),
                Some(&old.binding.review_id),
            ),
            Err(PublishError::SourceMismatch)
        ));
        assert_eq!(
            f.apply(3, Some(&old.binding.review_id)).unwrap(),
            PublishStatus::Updated
        );
        assert_eq!(
            f.apply(3, Some(&old.binding.review_id)).unwrap(),
            PublishStatus::Unchanged
        );
        let live = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
        assert_eq!(live.binding, binding(3));
        assert_eq!(
            fs::read(f.live().join("references/nested/note.txt")).unwrap(),
            [3, 0, 255]
        );
    }

    #[test]
    fn a_new_install_can_follow_removal_after_a_verified_rollback() {
        let (f, old) = rolled_back_update();
        let pending = scope(&f).1.latest().unwrap().unwrap().dir.path().to_owned();
        let receipt = pending.join("receipt.json");
        let sealed = fs::read(&receipt).unwrap();

        fs::remove_dir_all(f.live()).unwrap();
        for review in [2, 3] {
            assert!(matches!(
                f.apply(review, Some(&old.binding.review_id)),
                Err(PublishError::Changed)
            ));
        }
        assert_eq!(f.apply(3, None).unwrap(), PublishStatus::Installed);

        let live = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
        assert_eq!(live.binding, binding(3));
        assert_eq!(fs::read(receipt).unwrap(), sealed);
        for (path, data) in files(2) {
            assert_eq!(fs::read(pending.join("next").join(path)).unwrap(), data);
        }
    }

    #[test]
    fn a_new_install_can_follow_a_verified_unpublished_install() {
        let f = Fixture::new();
        let next = stage_initial_install(&f);
        assert!(next.is_dir());
        assert!(!f.live().exists());
        assert_eq!(f.apply(2, None).unwrap(), PublishStatus::Installed);
        let live = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
        assert_eq!(live.binding, binding(2));
        assert_eq!(
            fs::read(f.live().join("references/nested/note.txt")).unwrap(),
            [2, 0, 255]
        );
    }

    fn stage_initial_install(f: &Fixture) -> std::path::PathBuf {
        let root = f.root.project.pin().unwrap();
        let state = below(&root, &[".oah", "project-skills-v1"], true)
            .unwrap()
            .unwrap();
        state
            .with_lock(|| {
                let scope = Scope::open(&root, &state, Engine::Codex, &f.leaf, true)?.unwrap();
                let staged = scope.prepare(1, None, &binding(1), &files(1))?;
                Ok(staged.dir.path().join("next"))
            })
            .unwrap()
    }

    #[test]
    fn a_new_install_preserves_unverified_staging_and_foreign_entries() {
        for variant in [
            "staged edit",
            "missing stage",
            "unknown journal entry",
            "foreign leaf",
        ] {
            let f = Fixture::new();
            let next = stage_initial_install(&f);
            let keep = match variant {
                "staged edit" => next.join("SKILL.md"),
                "missing stage" => {
                    let moved = f.path.join("saved");
                    fs::rename(&next, &moved).unwrap();
                    moved.join("SKILL.md")
                }
                "unknown journal entry" => next.with_file_name("local-note"),
                "foreign leaf" => {
                    fs::create_dir(f.live()).unwrap();
                    f.live().join("SKILL.md")
                }
                _ => unreachable!(),
            };
            fs::write(&keep, b"user bytes").unwrap();
            let refused = f.apply(2, None);
            assert!(
                match variant {
                    "staged edit" | "missing stage" =>
                        matches!(refused, Err(PublishError::Changed)),
                    "unknown journal entry" => matches!(refused, Err(PublishError::InvalidReceipt)),
                    "foreign leaf" => matches!(refused, Err(PublishError::Occupied)),
                    _ => unreachable!(),
                },
                "{variant}: {refused:?}"
            );
            assert_eq!(fs::read(keep).unwrap(), b"user bytes");
            if variant != "foreign leaf" {
                assert!(!f.live().exists());
            }
        }
    }

    #[test]
    fn a_legacy_publication_needs_verified_live_bytes_before_reinstall_at_vacancy() {
        let f = Fixture::new();
        f.apply(1, None).unwrap();
        let prior = {
            let (state, scope) = scope(&f);
            state
                .with_lock(|| {
                    let old = scope.latest()?.unwrap().record.next;
                    let mut staged = scope.prepare(2, Some(old), &binding(2), &files(2))?;
                    staged.record.version = 1;
                    let receipt = Sealed {
                        sha256: staged.record.digest()?,
                        record: staged.record,
                    };
                    fs::write(
                        staged.dir.path().join("receipt.json"),
                        serde_json::to_vec(&receipt).unwrap(),
                    )?;
                    let staged = scope.latest()?.unwrap();
                    staged.retire(&scope)?;
                    staged.dir.move_new(
                        "next",
                        &scope.live,
                        &scope.target.leaf,
                        &staged.record.next.directory,
                    )?;
                    Ok(staged.dir.path().join("previous"))
                })
                .unwrap()
        };
        let live = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
        assert_eq!(live.binding, binding(2));
        let saved = f.path.join("saved-live");
        fs::rename(f.live(), &saved).unwrap();
        assert!(matches!(
            f.apply(3, None),
            Err(PublishError::Recovery { path, .. }) if path == prior
        ));
        assert_eq!(
            fs::read(prior.join("references/nested/note.txt")).unwrap(),
            [1, 0, 255]
        );
        fs::rename(&saved, f.live()).unwrap();
        assert_eq!(
            f.apply(2, Some(&binding(1).review_id)).unwrap(),
            PublishStatus::Unchanged
        );
        fs::remove_dir_all(f.live()).unwrap();
        assert_eq!(f.apply(3, None).unwrap(), PublishStatus::Installed);
        assert_eq!(
            fs::read(prior.join("SKILL.md")).unwrap(),
            files(1)["SKILL.md"]
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
