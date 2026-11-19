// 보호된 리눅스 네임스페이스에서 디렉터리 복사의 취득·검증·삭제를 기록한다.
use super::linux::{Journal, changed, destination_parent, new_reference};
use super::{FsxError, FsxErrorKind, ProtectedLinkRef, ReferenceKind, Removal};
use crate::fsx::links::source_in_stores;
use crate::fsx::staging::protected::{
    FILE_FLAGS, Namespace, canonical_absolute, descriptor, io, mode_only, probe, refuse,
};
use rustix::fd::{AsFd, OwnedFd};
use rustix::fs::{
    self as rx, AtFlags, Dir, Mode, OFlags, RenameFlags, ResolveFlags, Statx, StatxFlags,
};
use rustix::io::Errno;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fs::File;
use std::os::unix::{ffi::OsStrExt, fs::FileExt};
use std::path::{Path, PathBuf};

const MAGIC: &[u8] = b"oah-protected-copy-v1";
const MAX_ENTRIES: usize = 4096;
const MAX_DEPTH: usize = 32;
const MAX_RELATIVE: usize = 1024;
const MAX_DATA: u64 = 1 << 30;
const MAX_JOURNAL: u64 = 5 << 20;
const CHUNK: usize = 64 << 10;
const DIRECTORY: OFlags = OFlags::RDONLY.union(OFlags::DIRECTORY);
const STAGE_MODE: u16 = 0o100600;
const PROVISIONER: (u32, u32) = (0, 0);

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Directory,
    File,
}

fn kind(mode: u16) -> Option<Kind> {
    if mode & 0o022 != 0 {
        return None;
    }
    match mode & !0o777 {
        0o040000 if mode & 0o700 == 0o700 => Some(Kind::Directory),
        0o100000 if mode & 0o400 != 0 => Some(Kind::File),
        _ => None,
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct Identity {
    ino: u64,
    birth_seconds: i64,
    birth_nanos: u32,
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct Metadata {
    identity: Identity,
    mode: u16,
    size: u64,
    kind: Kind,
}

impl Metadata {
    fn read(stat: &Statx, owner: (u32, u32), mount: u64, path: &Path) -> Result<Self, FsxError> {
        let kind = kind(stat.stx_mode).ok_or_else(|| {
            refuse(
                path,
                "the entry type or permissions are unsupported for a copy",
            )
        })?;
        if (stat.stx_uid, stat.stx_gid) != owner
            || stat.stx_mnt_id != mount
            || !StatxFlags::from_bits_retain(stat.stx_mask).contains(StatxFlags::BTIME)
            || (kind == Kind::File && stat.stx_nlink != 1)
        {
            return Err(refuse(
                path,
                "copy entry owner, mount, birth time or link count is unverified",
            ));
        }
        Ok(Self {
            identity: Identity {
                ino: stat.stx_ino,
                birth_seconds: stat.stx_btime.tv_sec,
                birth_nanos: stat.stx_btime.tv_nsec,
            },
            mode: stat.stx_mode,
            size: if kind == Kind::File { stat.stx_size } else { 0 },
            kind,
        })
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct SourceEntry {
    metadata: Metadata,
    modified: (i64, u32),
    changed: (i64, u32),
}

impl SourceEntry {
    fn read(stat: &Statx, tree: &Tree<'_, '_>, path: &Path) -> Result<Self, FsxError> {
        Ok(Self {
            metadata: Metadata::read(stat, PROVISIONER, tree.namespace.mount, path)?,
            modified: (stat.stx_mtime.tv_sec, stat.stx_mtime.tv_nsec),
            changed: (stat.stx_ctime.tv_sec, stat.stx_ctime.tv_nsec),
        })
    }

    fn verify(&self, fd: &OwnedFd, tree: &Tree<'_, '_>, path: &Path) -> Result<(), FsxError> {
        if *self != Self::read(&descriptor(fd, path)?, tree, path)? {
            return Err(refuse(path, "the provisioned source changed"));
        }
        mode_only(fd, path, self.metadata.kind == Kind::Directory)
    }
}

struct Acquired {
    identity: Identity,
    mode: u16,
    contents: Contents,
}

enum Contents {
    Directory,
    File { size: u64, digest: [u8; 32] },
}

impl Acquired {
    fn metadata(&self) -> Metadata {
        let (kind, size) = match self.contents {
            Contents::Directory => (Kind::Directory, 0),
            Contents::File { size, .. } => (Kind::File, size),
        };
        Metadata {
            identity: self.identity,
            mode: self.mode,
            size,
            kind,
        }
    }
}

type Inventory = BTreeMap<PathBuf, Acquired>;

struct Tree<'a, 'path> {
    namespace: &'a Namespace<'path>,
    root: &'a Path,
    from_private: PathBuf,
}

impl<'a, 'path> Tree<'a, 'path> {
    fn new(namespace: &'a Namespace<'path>, root: &'a Path) -> Result<Self, FsxError> {
        let below_anchor = root
            .strip_prefix(namespace.anchor)
            .ok()
            .filter(|relative| !relative.as_os_str().is_empty() && canonical_absolute(root))
            .ok_or_else(|| {
                refuse(
                    root,
                    "expected a canonical descendant of the enrolled anchor",
                )
            })?;
        Ok(Self {
            namespace,
            root,
            from_private: Path::new("..").join(below_anchor),
        })
    }

    fn open(&self, relative: &Path, flags: OFlags, mode: Mode) -> Result<OwnedFd, FsxError> {
        let path = joined(self.root, relative);
        // The single parent step reaches the provisioner-owned anchor. Every remaining
        // component is canonical and all reopens stay on the enrolled mount.
        rx::openat2(
            &self.namespace.private,
            joined(&self.from_private, relative),
            FILE_FLAGS | flags,
            mode,
            ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV,
        )
        .map_err(|err| match err {
            Errno::NOENT | Errno::LOOP | Errno::XDEV | Errno::NOTDIR => refuse(
                &path,
                "the expected copy path is missing or outside the plain enrolled tree",
            ),
            _ => io(&path, "cannot open the confined copy path", err),
        })
    }

    fn owned_metadata(&self, stat: &Statx, path: &Path) -> Result<Metadata, FsxError> {
        Metadata::read(stat, self.namespace.writer(), self.namespace.mount, path)
    }

    fn verify_owned(&self, fd: &OwnedFd, path: &Path, expected: Metadata) -> Result<(), FsxError> {
        if self.owned_metadata(&descriptor(fd, path)?, path)? != expected {
            return Err(refuse(
                path,
                "the opened entry differs from the acquired inventory",
            ));
        }
        mode_only(fd, path, expected.kind == Kind::Directory)
    }

    fn visit(
        &self,
        relative: &Path,
        fd: OwnedFd,
        mut visit: impl FnMut(PathBuf, &Statx) -> Result<(), FsxError>,
    ) -> Result<(), FsxError> {
        let path = joined(self.root, relative);
        let mut directory = Dir::new(fd).map_err(|err| io(&path, "cannot read directory", err))?;
        while let Some(item) = directory.read() {
            let item = item.map_err(|err| io(&path, "cannot enumerate directory", err))?;
            let raw = item.file_name().to_bytes();
            if matches!(raw, b"." | b"..") {
                continue;
            }
            let name = OsStr::from_bytes(raw);
            let child = joined(relative, Path::new(name));
            let display = self.root.join(&child);
            if !valid_relative(child.as_os_str().as_bytes()) {
                return Err(refuse(
                    &display,
                    "the copy inventory path exceeds its bounds",
                ));
            }
            let fd = directory
                .fd()
                .map_err(|err| io(&path, "cannot inspect directory", err))?;
            visit(child, &required_entry(&fd, name, &display)?)?;
        }
        Ok(())
    }
}

fn joined(root: &Path, relative: &Path) -> PathBuf {
    if relative.as_os_str().is_empty() {
        root.to_path_buf()
    } else {
        root.join(relative)
    }
}

fn valid_relative(raw: &[u8]) -> bool {
    raw.is_empty()
        || (raw.len() <= MAX_RELATIVE
            && raw.split(|&byte| byte == b'/').count() <= MAX_DEPTH
            && raw.split(|&byte| byte == b'/').all(|name| {
                !matches!(name, b"" | b"." | b"..") && name.len() <= 255 && !name.contains(&0)
            }))
}

fn required_entry(fd: &impl AsFd, name: &OsStr, path: &Path) -> Result<Statx, FsxError> {
    probe(fd, name, path)?.ok_or_else(|| refuse(path, "an expected copy entry is missing"))
}

fn location(path: &Path) -> Result<(&Path, &OsStr), FsxError> {
    path.parent()
        .zip(path.file_name())
        .ok_or_else(|| refuse(path, "missing copy entry name or parent"))
}

fn source_inventory(tree: &Tree<'_, '_>) -> Result<BTreeMap<PathBuf, SourceEntry>, FsxError> {
    let namespace = tree.namespace;
    let empty = Path::new("");
    let ancestors: Vec<_> = tree
        .root
        .ancestors()
        .skip(1)
        .take_while(|&path| path != namespace.anchor)
        .collect();
    for &path in ancestors.iter().rev() {
        let ancestor = Tree::new(namespace, path)?;
        let fd = ancestor.open(empty, DIRECTORY, Mode::empty())?;
        SourceEntry::read(&descriptor(&fd, path)?, &ancestor, path)?;
        mode_only(&fd, path, true)?;
    }
    let root = {
        let fd = tree.open(empty, DIRECTORY, Mode::empty())?;
        SourceEntry::read(&descriptor(&fd, tree.root)?, tree, tree.root)?
    };
    let mut entries = BTreeMap::from([(PathBuf::new(), root)]);
    let mut directories = vec![PathBuf::new()];
    let mut bytes = 0u64;
    while let Some(relative) = directories.pop() {
        let path = joined(tree.root, &relative);
        let entry = entries
            .get(&relative)
            .ok_or_else(|| refuse(&path, "missing source entry"))?;
        let fd = tree.open(&relative, DIRECTORY, Mode::empty())?;
        entry.verify(&fd, tree, &path)?;
        tree.visit(&relative, fd, |child, stat| {
            let display = tree.root.join(&child);
            let entry = SourceEntry::read(stat, tree, &display)?;
            bytes = bytes.saturating_add(entry.metadata.size);
            if entries.len() == MAX_ENTRIES || bytes > MAX_DATA {
                return Err(refuse(
                    &display,
                    "the source exceeds the copy inventory or byte limit",
                ));
            }
            if entry.metadata.kind == Kind::Directory {
                directories.push(child.clone());
            }
            if entries.insert(child, entry).is_some() {
                return Err(refuse(&display, "a source name appeared twice"));
            }
            Ok(())
        })?;
    }
    for (relative, entry) in &entries {
        if entry.metadata.kind == Kind::File {
            let fd = tree.open(relative, OFlags::RDONLY, Mode::empty())?;
            entry.verify(&fd, tree, &tree.root.join(relative))?;
        }
    }
    Ok(entries)
}

fn set_mode(fd: &OwnedFd, path: &Path, mode: u16) -> Result<(), FsxError> {
    rx::fchmod(fd, Mode::from_raw_mode(u32::from(mode & 0o777)))
        .map_err(|err| io(path, "cannot set copy permissions", err))
}

fn sync(fd: &OwnedFd, path: &Path, action: &str) -> Result<(), FsxError> {
    rx::fsync(fd).map_err(|err| io(path, action, err))
}

fn acquire_directory(
    stage: &Tree<'_, '_>,
    relative: &Path,
    mode: u16,
    inventory: &Inventory,
) -> Result<Acquired, FsxError> {
    let display = joined(stage.root, relative);
    let create = |parent: &OwnedFd, name: &OsStr| -> Result<Metadata, FsxError> {
        rx::mkdirat(parent, name, Mode::RWXU)
            .map_err(|err| io(&display, "cannot exclusively create a copy directory", err))?;
        stage.owned_metadata(&required_entry(parent, name, &display)?, &display)
    };
    let acquired = if relative.as_os_str().is_empty() {
        create(&stage.namespace.private, location(stage.root)?.1)?
    } else {
        let (parent, name) = location(relative)?;
        let fd = open_acquired_directory(stage, inventory, parent)?;
        create(&fd, name)?
    };
    let fd = stage.open(relative, DIRECTORY, Mode::empty())?;
    stage.verify_owned(&fd, &display, acquired)?;
    set_mode(&fd, &display, mode)?;
    stage.verify_owned(&fd, &display, Metadata { mode, ..acquired })?;
    Ok(Acquired {
        identity: acquired.identity,
        mode,
        contents: Contents::Directory,
    })
}

fn open_acquired_directory(
    tree: &Tree<'_, '_>,
    inventory: &Inventory,
    relative: &Path,
) -> Result<OwnedFd, FsxError> {
    let display = joined(tree.root, relative);
    let entry = inventory
        .get(relative)
        .filter(|entry| matches!(entry.contents, Contents::Directory))
        .ok_or_else(|| refuse(&display, "the copy directory was not acquired"))?;
    let fd = tree.open(relative, DIRECTORY, Mode::empty())?;
    tree.verify_owned(&fd, &display, entry.metadata())?;
    Ok(fd)
}

fn acquire_file(
    source: &Tree<'_, '_>,
    stage: &Tree<'_, '_>,
    relative: &Path,
    entry: SourceEntry,
    buffer: &mut [u8; CHUNK],
) -> Result<Acquired, FsxError> {
    let input_path = source.root.join(relative);
    let output_path = stage.root.join(relative);
    let output = stage.open(
        relative,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL,
        Mode::RUSR | Mode::WUSR,
    )?;
    set_mode(&output, &output_path, STAGE_MODE)?;
    let created = stage.owned_metadata(&descriptor(&output, &output_path)?, &output_path)?;
    let mut initial = Metadata {
        identity: created.identity,
        mode: STAGE_MODE,
        size: 0,
        kind: Kind::File,
    };
    stage.verify_owned(&output, &output_path, initial)?;
    drop(output);

    let expected_size = entry.metadata.size;
    let mut digest = Sha256::new();
    while initial.size < expected_size {
        let offset = initial.size;
        let chunk = chunk(buffer, expected_size - offset);
        let input = source.open(relative, OFlags::RDONLY, Mode::empty())?;
        entry.verify(&input, source, &input_path)?;
        let input = File::from(input);
        input
            .read_exact_at(chunk, offset)
            .map_err(|err| io(&input_path, "cannot read the source chunk", err))?;
        entry.verify(&input.into(), source, &input_path)?;

        let output = stage.open(relative, OFlags::WRONLY, Mode::empty())?;
        stage.verify_owned(&output, &output_path, initial)?;
        File::from(output)
            .write_all_at(chunk, offset)
            .map_err(|err| io(&output_path, "cannot write the copy chunk", err))?;
        digest.update(&*chunk);
        initial.size += chunk.len() as u64;
    }
    let output = stage.open(relative, OFlags::WRONLY, Mode::empty())?;
    stage.verify_owned(&output, &output_path, initial)?;
    let mode = entry.metadata.mode;
    set_mode(&output, &output_path, mode)?;
    sync(&output, &output_path, "cannot sync the staged file")?;
    stage.verify_owned(&output, &output_path, Metadata { mode, ..initial })?;
    Ok(Acquired {
        identity: initial.identity,
        mode,
        contents: Contents::File {
            size: expected_size,
            digest: digest.finalize().into(),
        },
    })
}

fn chunk(buffer: &mut [u8; CHUNK], remaining: u64) -> &mut [u8] {
    let available = usize::try_from(remaining).unwrap_or(CHUNK);
    &mut buffer[..CHUNK.min(available)]
}

fn verify_tree(tree: &Tree<'_, '_>, inventory: &Inventory) -> Result<(), FsxError> {
    let mut seen = 1;
    for (relative, entry) in inventory {
        if !matches!(entry.contents, Contents::Directory) {
            continue;
        }
        let fd = open_acquired_directory(tree, inventory, relative)?;
        tree.visit(relative, fd, |child, stat| {
            let path = tree.root.join(&child);
            let expected = inventory.get(&child).ok_or_else(|| {
                refuse(
                    &path,
                    "the copy contains an entry outside the acquired inventory",
                )
            })?;
            if tree.owned_metadata(stat, &path)? != expected.metadata() {
                return Err(refuse(
                    &path,
                    "the copy entry differs from the acquired inventory",
                ));
            }
            if seen == inventory.len() {
                return Err(refuse(
                    &path,
                    "the copy contains more entries than were acquired",
                ));
            }
            seen += 1;
            Ok(())
        })?;
    }
    if seen != inventory.len() {
        return Err(refuse(
            tree.root,
            "the copy is missing entries from the acquired inventory",
        ));
    }
    let mut buffer = [0; CHUNK];
    for (relative, entry) in inventory {
        if let Contents::File {
            size,
            digest: expected,
        } = entry.contents
        {
            let path = tree.root.join(relative);
            let fd = tree.open(relative, OFlags::RDONLY, Mode::empty())?;
            tree.verify_owned(&fd, &path, entry.metadata())?;
            let input = File::from(fd);
            let mut digest = Sha256::new();
            let mut offset = 0;
            while offset < size {
                let chunk = chunk(&mut buffer, size - offset);
                input
                    .read_exact_at(chunk, offset)
                    .map_err(|err| io(&path, "cannot read back the copy file", err))?;
                digest.update(&*chunk);
                offset += chunk.len() as u64;
            }
            let actual: [u8; 32] = digest.finalize().into();
            if actual != expected {
                return Err(refuse(
                    &path,
                    "the copy file bytes differ from the acquired inventory",
                ));
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum Phase {
    Preparing,
    Ready,
    Active,
    Removing,
    Retired,
}

struct Record<'a> {
    source: &'a Path,
    destination: &'a Path,
    inventory: Inventory,
    phase: Phase,
}

fn field<'a>(bytes: &mut &'a [u8]) -> Option<&'a [u8]> {
    let end = bytes.iter().position(|byte| *byte == 0)?;
    let (field, tail) = bytes.split_at(end);
    *bytes = tail.get(1..)?;
    Some(field)
}

fn take<const N: usize>(bytes: &mut &[u8]) -> Option<[u8; N]> {
    let (field, tail) = bytes.split_first_chunk::<N>()?;
    *bytes = tail;
    Some(*field)
}

fn encode_inventory(inventory: &Inventory) -> Result<Vec<u8>, FsxError> {
    let count = u32::try_from(inventory.len()).map_err(|_| {
        refuse(
            Path::new("<copy inventory>"),
            "copy entry count exceeds the record format",
        )
    })?;
    let mut bytes = vec![b'A'];
    bytes.extend_from_slice(&count.to_be_bytes());
    for (path, entry) in inventory {
        bytes.extend_from_slice(path.as_os_str().as_bytes());
        bytes.push(0);
        bytes.extend_from_slice(&entry.mode.to_be_bytes());
        bytes.extend_from_slice(&entry.identity.ino.to_be_bytes());
        bytes.extend_from_slice(&entry.identity.birth_seconds.to_be_bytes());
        bytes.extend_from_slice(&entry.identity.birth_nanos.to_be_bytes());
        if let Contents::File { size, digest } = entry.contents {
            bytes.extend_from_slice(&size.to_be_bytes());
            bytes.extend_from_slice(&digest);
        }
    }
    Ok(bytes)
}

fn decode_inventory(bytes: &mut &[u8]) -> Option<Inventory> {
    let count = usize::try_from(u32::from_be_bytes(take(bytes)?)).ok()?;
    if !(1..=MAX_ENTRIES).contains(&count) {
        return None;
    }
    let mut inventory: Inventory = BTreeMap::new();
    let mut total = 0u64;
    for index in 0..count {
        let name = field(bytes)?;
        let path = Path::new(OsStr::from_bytes(name));
        let mode = u16::from_be_bytes(take(bytes)?);
        let kind = kind(mode)?;
        if !valid_relative(name)
            || if index == 0 {
                !name.is_empty() || kind != Kind::Directory
            } else {
                !matches!(inventory.get(path.parent()?)?.contents, Contents::Directory)
            }
        {
            return None;
        }
        let identity = Identity {
            ino: u64::from_be_bytes(take(bytes)?),
            birth_seconds: i64::from_be_bytes(take(bytes)?),
            birth_nanos: u32::from_be_bytes(take(bytes)?),
        };
        let contents = match kind {
            Kind::Directory => Contents::Directory,
            Kind::File => {
                let size = u64::from_be_bytes(take(bytes)?);
                total = total.checked_add(size)?;
                Contents::File {
                    size,
                    digest: take(bytes)?,
                }
            }
        };
        let entry = Acquired {
            identity,
            mode,
            contents,
        };
        if identity.birth_nanos >= 1_000_000_000
            || total > MAX_DATA
            || inventory.insert(path.to_path_buf(), entry).is_some()
        {
            return None;
        }
    }
    Some(inventory)
}

fn decode<'a>(
    mut bytes: &'a [u8],
    namespace: &Namespace<'_>,
    journal: &Journal<'_, '_>,
) -> Option<Record<'a>> {
    if field(&mut bytes)? != MAGIC
        || field(&mut bytes)? != namespace.incarnation().to_hex().as_bytes()
        || field(&mut bytes)? != journal.id.as_bytes()
    {
        return None;
    }
    let source = Path::new(OsStr::from_bytes(field(&mut bytes)?));
    let destination = Path::new(OsStr::from_bytes(field(&mut bytes)?));
    let (inventory, phase) = if bytes.is_empty() {
        (Inventory::new(), Phase::Preparing)
    } else {
        bytes = bytes.strip_prefix(b"A")?;
        let inventory = decode_inventory(&mut bytes)?;
        let phase = match bytes {
            b"" => Phase::Ready,
            b"P" => Phase::Active,
            b"PD" => Phase::Removing,
            b"PDX" => Phase::Retired,
            _ => return None,
        };
        (inventory, phase)
    };
    Some(Record {
        source,
        destination,
        inventory,
        phase,
    })
}

pub(super) fn create(
    anchor: &Path,
    source: &Path,
    destination: &Path,
) -> Result<ProtectedLinkRef, FsxError> {
    destination_parent(anchor, destination)?;
    let namespace = Namespace::open(anchor)?;
    create_locked(&namespace, source, destination)
}

pub(super) fn create_locked(
    namespace: &Namespace<'_>,
    source: &Path,
    destination: &Path,
) -> Result<ProtectedLinkRef, FsxError> {
    let (_, leaf) = location(destination)?;
    if probe(&namespace.public_folder()?, leaf, destination)?.is_some() {
        return Err(FsxError::new(
            FsxErrorKind::LinkExists,
            destination,
            "the destination is occupied",
        ));
    }
    let source_tree = Tree::new(namespace, source)?;
    let sources = source_inventory(&source_tree)?;
    let reference = new_reference(namespace, ReferenceKind::Copy)?;
    let journal = Journal::new(namespace, &reference);
    let stage_root = namespace.private_path(&journal.pin);
    let stage = Tree::new(namespace, &stage_root)?;
    let published = Tree::new(namespace, destination)?;
    let unconfirmed = |err| {
        changed(
            err,
            destination,
            "copy acquisition is unconfirmed; inspect the ownership journal",
        )
    };
    let header = journal.header(MAGIC, source, destination);
    journal.prepare_record(&header).map_err(|error| {
        if error.kind() == FsxErrorKind::NotOwned {
            error
        } else {
            unconfirmed(error)
        }
    })?;

    let mut inventory = Inventory::new();
    let mut buffer = [0; CHUNK];
    for (relative, entry) in sources {
        let acquired = match entry.metadata.kind {
            Kind::Directory => {
                acquire_directory(&stage, &relative, entry.metadata.mode, &inventory)
            }
            Kind::File => acquire_file(&source_tree, &stage, &relative, entry, &mut buffer),
        }
        .map_err(unconfirmed)?;
        inventory.insert(relative, acquired);
    }
    for (relative, entry) in &inventory {
        if matches!(entry.contents, Contents::Directory) {
            let fd = open_acquired_directory(&stage, &inventory, relative).map_err(unconfirmed)?;
            sync(
                &fd,
                &joined(stage.root, relative),
                "cannot sync the staged directory",
            )
            .map_err(unconfirmed)?;
        }
    }
    sync(
        &namespace.private,
        &stage_root,
        "cannot sync private staging",
    )
    .map_err(unconfirmed)?;
    journal
        .append(&encode_inventory(&inventory).map_err(unconfirmed)?)
        .map_err(unconfirmed)?;

    let folder = namespace.public_folder().map_err(unconfirmed)?;
    publish(&namespace.private, OsStr::new(&journal.pin), &folder, leaf).map_err(|err| {
        unconfirmed(io(
            destination,
            "cannot publish the copy without replacing an entry",
            err,
        ))
    })?;
    sync(
        &folder,
        destination,
        "cannot sync the published copy directory",
    )
    .map_err(unconfirmed)?;
    drop(folder);
    sync(
        &namespace.private,
        &stage_root,
        "cannot sync private staging after publication",
    )
    .map_err(unconfirmed)?;
    verify_tree(&published, &inventory).map_err(unconfirmed)?;
    journal.append(b"P").map_err(unconfirmed)?;
    Ok(reference)
}

fn publish(
    private: &OwnedFd,
    staged: &OsStr,
    public: &OwnedFd,
    leaf: &OsStr,
) -> rustix::io::Result<()> {
    rx::renameat_with(private, staged, public, leaf, RenameFlags::NOREPLACE)
}

pub(super) fn remove(
    namespace: &Namespace<'_>,
    journal: &Journal<'_, '_>,
    stores: &[&Path],
) -> Result<Removal, FsxError> {
    let bytes = journal.read(MAX_JOURNAL)?;
    let record = decode(&bytes, namespace, journal).ok_or_else(|| {
        refuse(
            namespace.anchor,
            "unrecognized or incomplete copy ownership journal",
        )
    })?;
    let destination = record.destination;
    destination_parent(namespace.anchor, destination)?;
    let (_, leaf) = location(destination)?;
    Tree::new(namespace, record.source)?;
    if !source_in_stores(record.source, stores) {
        return Err(refuse(
            destination,
            "the recorded source is outside the known stores",
        ));
    }
    match record.phase {
        Phase::Preparing | Phase::Ready | Phase::Removing => {
            return Err(refuse(
                destination,
                "copy acquisition or removal was interrupted; inspect the ownership journal",
            ));
        }
        Phase::Active | Phase::Retired => {}
    }
    let stage = namespace.private_path(&journal.pin);
    if probe(&namespace.private, OsStr::new(&journal.pin), &stage)?.is_some() {
        return Err(refuse(&stage, "the copy has an unexpected private stage"));
    }
    let present = probe(&namespace.public_folder()?, leaf, destination)?.is_some();
    if matches!(record.phase, Phase::Retired) {
        return if present {
            Err(refuse(
                destination,
                "a retired copy reference cannot authorize deletion",
            ))
        } else {
            Ok(Removal::AlreadyGone)
        };
    }
    if !present {
        return Err(refuse(
            destination,
            "the active copy root is missing; inspect the ownership journal",
        ));
    }
    let tree = Tree::new(namespace, destination)?;
    verify_tree(&tree, &record.inventory)?;
    journal.append(b"D")?;
    let incomplete = |err| {
        changed(
            err,
            destination,
            "copy removal is incomplete; inspect the ownership journal",
        )
    };
    for (relative, entry) in record.inventory.iter().rev() {
        unlink(&tree, &record.inventory, relative, entry.metadata()).map_err(incomplete)?;
    }
    journal.append(b"X").map_err(incomplete)?;
    Ok(Removal::Removed)
}

fn unlink(
    tree: &Tree<'_, '_>,
    inventory: &Inventory,
    relative: &Path,
    expected: Metadata,
) -> Result<(), FsxError> {
    let path = joined(tree.root, relative);
    let (parent, name) = if relative.as_os_str().is_empty() {
        (tree.namespace.public_folder()?, location(tree.root)?.1)
    } else {
        let (parent, name) = location(relative)?;
        (open_acquired_directory(tree, inventory, parent)?, name)
    };
    if tree.owned_metadata(&required_entry(&parent, name, &path)?, &path)? != expected {
        return Err(refuse(
            &path,
            "the entry to unlink differs from the acquired inventory",
        ));
    }
    let flags = match expected.kind {
        Kind::Directory => AtFlags::REMOVEDIR,
        Kind::File => AtFlags::empty(),
    };
    rx::unlinkat(&parent, name, flags)
        .map_err(|err| io(&path, "cannot unlink the acquired copy entry", err))?;
    sync(
        &parent,
        &path,
        "cannot sync the parent after unlinking the copy entry",
    )
}

#[cfg(test)]
mod tests {
    use super::{DIRECTORY, Errno, FILE_FLAGS, Mode, OsStr, publish, rx};

    #[test]
    fn publication_refuses_an_ordinary_occupied_empty_directory() {
        let fixture = tempfile::tempdir().unwrap();
        let staging = fixture.path().join("staging");
        let public = fixture.path().join("public");
        std::fs::create_dir_all(staging.join("staged")).unwrap();
        std::fs::create_dir_all(public.join("occupied")).unwrap();
        std::fs::write(staging.join("staged/keep"), b"staged bytes").unwrap();

        let from = rx::open(&staging, DIRECTORY | FILE_FLAGS, Mode::empty()).unwrap();
        let to = rx::open(&public, DIRECTORY | FILE_FLAGS, Mode::empty()).unwrap();
        assert_eq!(
            publish(&from, OsStr::new("staged"), &to, OsStr::new("occupied")),
            Err(Errno::EXIST)
        );
        assert_eq!(
            std::fs::read_dir(public.join("occupied")).unwrap().count(),
            0
        );
        assert_eq!(
            std::fs::read(staging.join("staged/keep")).unwrap(),
            b"staged bytes"
        );
    }
}
