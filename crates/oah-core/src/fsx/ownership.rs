use super::links::{LinkRequest, Removal};
use super::{Fingerprint, FsxError, FsxErrorKind};
use std::fmt;
use std::path::Path;

const REFERENCE_VERSION: &str = "oah-link-v1";

/// Persistable reference to a native symlink acquired in an enrolled namespace.
#[derive(Clone, PartialEq, Eq)]
pub struct ProtectedLinkRef {
    incarnation: Fingerprint,
    secret: Fingerprint,
}

impl ProtectedLinkRef {
    /// Encodes the reference for the caller's persistent record.
    pub fn encode(&self) -> String {
        format!(
            "{REFERENCE_VERSION}:{}:{}",
            self.incarnation.to_hex(),
            self.secret.to_hex()
        )
    }

    /// Restores a reference without opening the namespace or granting deletion authority.
    ///
    /// # Errors
    /// Returns [`FsxErrorKind::NotOwned`] if the reference is malformed or has an unknown version.
    pub fn restore(text: &str) -> Result<Self, FsxError> {
        let parse = || {
            let body = text.strip_prefix(REFERENCE_VERSION)?.strip_prefix(':')?;
            let (incarnation, secret) = body.split_once(':')?;
            Some(Self {
                incarnation: Fingerprint::from_hex(incarnation)?,
                secret: Fingerprint::from_hex(secret)?,
            })
        };
        parse().ok_or_else(|| {
            FsxError::new(
                FsxErrorKind::NotOwned,
                Path::new("<link reference>"),
                "unrecognized protected link reference",
            )
        })
    }
}

impl fmt::Debug for ProtectedLinkRef {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("ProtectedLinkRef(v1)")
    }
}

/// Publishes a native symlink under an externally enrolled Linux anchor.
///
/// The destination is a direct child of `anchor/links`. Both request paths use canonical syntax.
/// The reference is issued after publication read-back and journal sync. This entry point uses
/// native symlinks for both target styles.
///
/// # Enrollment
/// A trusted provisioner supplies an ext4 anchor and root-owned 0755 ancestors. The anchor
/// contains writer-owned `links` (0755) and `.oah-links` (0700) directories, and a root-owned
/// 0644 `.oah-enrollment` file. Its six lines are `oah-protected-links-v1`, the nonzero writer
/// UID and GID separated by a space, a fresh random 64-digit lowercase hex incarnation, then
/// the anchor, public and private directory bindings in that order. A binding is five decimal
/// fields separated by spaces from Linux statx: device major, device minor, inode, birth seconds
/// and birth nanoseconds. All three directories and the enrollment file share one mount.
///
/// Keep the mount namespace, procfs and mount administration trusted and the writer UID dedicated
/// to authorized code. Use ordinary unmapped ownership and mode bits. The writer runs with
/// matching real, effective, saved and filesystem IDs, no active privileges and no-new-privileges.
/// Reprovisioning, restoring state from backup, or losing proof requires a new incarnation.
///
/// # Errors
/// Unsupported enrollment is refused. Publication or sync failures can leave a private stage,
/// a journal and a published entry for inspection. An error returns no usable reference.
pub fn create_protected(
    anchor: &Path,
    request: &LinkRequest<'_>,
) -> Result<ProtectedLinkRef, FsxError> {
    #[cfg(target_os = "linux")]
    {
        linux::create(anchor, request)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = request;
        Err(FsxError::new(
            FsxErrorKind::NotOwned,
            anchor,
            "protected managed links require Linux",
        ))
    }
}

/// Removes the enrolled acquisition identified by a restored reference.
///
/// The recorded source must be in `stores`. A retired reference reports [`Removal::AlreadyGone`]
/// when its destination is absent. The link target is preserved.
///
/// # Errors
/// Refuses unsupported authority, unknown proof or an interrupted transaction. If deletion starts
/// and fails, the returned error identifies the failed step and the journal bars automatic retry.
pub fn remove_protected(
    anchor: &Path,
    reference: &ProtectedLinkRef,
    stores: &[&Path],
) -> Result<Removal, FsxError> {
    #[cfg(target_os = "linux")]
    {
        linux::remove(anchor, reference, stores)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (reference, stores);
        Err(FsxError::new(
            FsxErrorKind::NotOwned,
            anchor,
            "protected managed links require Linux",
        ))
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::{
        Fingerprint, FsxError, FsxErrorKind, LinkRequest, Path, ProtectedLinkRef, Removal,
    };
    use crate::fsx::links::{TargetStyle, relative_between, source_in_stores};
    use crate::fsx::staging::protected::{
        FILE_FLAGS, Namespace, PUBLIC, canonical_absolute, descriptor, io, probe, refuse,
    };
    use rustix::fd::OwnedFd;
    use rustix::fs::{self as rx, AtFlags, Mode, OFlags};
    use rustix::io::Errno;
    use std::ffi::OsStr;
    use std::fs::{self, File};
    use std::io::{Read, Write};
    use std::os::unix::ffi::OsStrExt;
    use std::path::PathBuf;

    const JOURNAL_MAGIC: &[u8] = b"oah-native-symlink-v1";
    const MAX_JOURNAL: u64 = 16_384;

    struct LinkPaths {
        source: PathBuf,
        destination: PathBuf,
        target: PathBuf,
    }

    impl LinkPaths {
        fn leaf(&self) -> Result<&OsStr, FsxError> {
            self.destination
                .file_name()
                .ok_or_else(|| refuse(&self.destination, "missing destination leaf"))
        }
    }

    fn destination_parent<'a>(anchor: &Path, destination: &'a Path) -> Result<&'a Path, FsxError> {
        destination
            .parent()
            .filter(|parent| {
                canonical_absolute(destination)
                    && parent.as_os_str() == anchor.join(PUBLIC).as_os_str()
            })
            .ok_or_else(|| {
                refuse(
                    destination,
                    "expected a canonical direct child of the enrolled links directory",
                )
            })
    }

    #[derive(Clone, Copy)]
    enum Phase {
        Prepared,
        Acquired,
        Active(u64),
        Removing,
        Retired,
    }

    struct Journal<'a, 'path> {
        namespace: &'a Namespace<'path>,
        id: String,
        file: String,
        pin: String,
    }

    impl<'a, 'path> Journal<'a, 'path> {
        fn new(namespace: &'a Namespace<'path>, reference: &ProtectedLinkRef) -> Self {
            let id = Fingerprint::of(reference.secret.as_bytes()).to_hex();
            Self {
                namespace,
                file: format!("{id}.log"),
                pin: format!("{id}.pin"),
                id,
            }
        }

        fn prepare(&self, paths: &LinkPaths) -> Result<(), FsxError> {
            let path = self.namespace.private_path(&self.file);
            let pin = self.namespace.private_path(&self.pin);
            if probe(&self.namespace.private, OsStr::new(&self.pin), &pin)?.is_some() {
                return Err(refuse(&pin, "the acquisition's staging name is occupied"));
            }
            let incarnation = self.namespace.incarnation().to_hex();
            let mut record = Vec::new();
            for field in [
                JOURNAL_MAGIC,
                incarnation.as_bytes(),
                self.id.as_bytes(),
                paths.source.as_os_str().as_bytes(),
                paths.destination.as_os_str().as_bytes(),
                paths.target.as_os_str().as_bytes(),
            ] {
                record.extend_from_slice(field);
                record.push(0);
            }
            let fd = rx::openat(
                &self.namespace.private,
                &self.file,
                FILE_FLAGS | OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL,
                Mode::RUSR | Mode::WUSR,
            )
            .map_err(|err| match err {
                Errno::EXIST => refuse(&path, "the acquisition journal already exists"),
                _ => io(&path, "cannot create the acquisition journal", err),
            })?;
            rx::fchmod(&fd, Mode::RUSR | Mode::WUSR)
                .map_err(|err| io(&path, "cannot set the acquisition journal mode", err))?;
            persist(File::from(fd), &record)
                .and_then(|()| rx::fsync(&self.namespace.private).map_err(Into::into))
                .map_err(|err| io(&path, "cannot persist preparation before staging", err))
        }

        fn load(&self) -> Result<(LinkPaths, Phase), FsxError> {
            let path = self.namespace.private_path(&self.file);
            let mut bytes = Vec::new();
            self.open(OFlags::RDONLY)?
                .take(MAX_JOURNAL + 1)
                .read_to_end(&mut bytes)
                .map_err(|err| io(&path, "cannot read the ownership journal", err))?;
            let parse = || {
                let [magic, incarnation, id, source, destination, target, phase] = bytes
                    .splitn(7, |&byte| byte == 0)
                    .collect::<Vec<_>>()
                    .try_into()
                    .ok()?;
                if bytes.len() > 16_384
                    || magic != JOURNAL_MAGIC
                    || incarnation != self.namespace.incarnation().to_hex().as_bytes()
                    || id != self.id.as_bytes()
                    || target.is_empty()
                    || target.contains(&0)
                {
                    return None;
                }
                let paths = LinkPaths {
                    source: OsStr::from_bytes(source).into(),
                    destination: OsStr::from_bytes(destination).into(),
                    target: OsStr::from_bytes(target).into(),
                };
                let phase = match phase {
                    [] => Phase::Prepared,
                    [b'A', _, _, _, _, _, _, _, _] => Phase::Acquired,
                    [b'A', inode @ .., b'P'] if inode.len() == 8 => {
                        Phase::Active(u64::from_be_bytes(inode.try_into().ok()?))
                    }
                    [b'A', inode @ .., b'P', b'D'] if inode.len() == 8 => Phase::Removing,
                    [b'A', inode @ .., b'P', b'D', b'X'] if inode.len() == 8 => Phase::Retired,
                    _ => return None,
                };
                Some((paths, phase))
            };
            parse().ok_or_else(|| refuse(&path, "unrecognized or incomplete ownership journal"))
        }

        fn append(&self, transition: &[u8]) -> Result<(), FsxError> {
            let path = self.namespace.private_path(&self.file);
            persist(self.open(OFlags::WRONLY | OFlags::APPEND)?, transition)
                .map_err(|err| io(&path, "cannot sync an ownership transition", err))
        }

        fn open(&self, access: OFlags) -> Result<File, FsxError> {
            let path = self.namespace.private_path(&self.file);
            let directory = &self.namespace.private;
            let valid = |stat: &rx::Statx| {
                (stat.stx_uid, stat.stx_gid) == self.namespace.writer()
                    && stat.stx_mode == 0o100600
                    && stat.stx_nlink == 1
                    && stat.stx_mnt_id == self.namespace.mount
            };
            if !probe(directory, OsStr::new(&self.file), &path)?
                .as_ref()
                .is_some_and(valid)
            {
                return Err(refuse(
                    &path,
                    "the ownership journal is missing or unrecognized",
                ));
            }
            let fd = rx::openat(directory, &self.file, FILE_FLAGS | access, Mode::empty())
                .map_err(|err| io(&path, "cannot open the ownership journal", err))?;
            if !valid(&descriptor(&fd, &path)?) {
                return Err(refuse(&path, "the opened journal is unrecognized"));
            }
            Ok(File::from(fd))
        }

        fn observed_link(
            &self,
            directory: &OwnedFd,
            name: &OsStr,
            display: &Path,
            target: &Path,
        ) -> Result<Option<u64>, FsxError> {
            let Some(stat) = probe(directory, name, display)? else {
                return Ok(None);
            };
            if stat.stx_mnt_id != self.namespace.mount
                || (stat.stx_uid, stat.stx_gid) != self.namespace.writer()
                || stat.stx_mode & 0o170000 != 0o120000
            {
                return Err(refuse(
                    display,
                    "expected an enrolled symlink on the same mount",
                ));
            }
            let found = rx::readlinkat(directory, name, Vec::new())
                .map_err(|err| io(display, "cannot read the symlink's stored target", err))?;
            if found.to_bytes() != target.as_os_str().as_bytes() {
                return Err(refuse(
                    display,
                    "the symlink's stored target differs from the acquisition",
                ));
            }
            Ok(Some(stat.stx_ino))
        }
    }

    fn persist(mut file: File, bytes: &[u8]) -> std::io::Result<()> {
        file.write_all(bytes)?;
        file.sync_all()
    }

    fn secret() -> Result<Fingerprint, FsxError> {
        let path = Path::new("/dev/urandom");
        let fd = rx::open(path, FILE_FLAGS | OFlags::RDONLY, Mode::empty())
            .map_err(|err| io(path, "cannot open the kernel random device", err))?;
        let stat = descriptor(&fd, path)?;
        if (
            stat.stx_mode & 0o170000,
            stat.stx_rdev_major,
            stat.stx_rdev_minor,
        ) != (0o020000, 1, 9)
        {
            return Err(refuse(path, "unrecognized random device"));
        }
        let mut bytes = [0; 32];
        File::from(fd)
            .read_exact(&mut bytes)
            .map_err(|err| io(path, "cannot read the kernel random device", err))?;
        Ok(Fingerprint::of(&bytes))
    }

    fn changed(error: FsxError, destination: &Path, what: &str) -> FsxError {
        match error.io_error() {
            Some(cause) => {
                let cause = cause.raw_os_error().map_or_else(
                    || std::io::Error::new(cause.kind(), cause.to_string()),
                    std::io::Error::from_raw_os_error,
                );
                FsxError::io(
                    FsxErrorKind::Unverified,
                    destination,
                    &format!("{what}: {error}"),
                    cause,
                )
            }
            None => FsxError::new(
                FsxErrorKind::Unverified,
                destination,
                format!("{what}: {error}"),
            ),
        }
    }

    pub(super) fn create(
        anchor: &Path,
        request: &LinkRequest<'_>,
    ) -> Result<ProtectedLinkRef, FsxError> {
        let destination = request.destination;
        let parent = destination_parent(anchor, destination)?;
        let source = request.source;
        if !canonical_absolute(source) {
            return Err(refuse(source, "expected a canonical absolute source"));
        }
        let namespace = Namespace::open(anchor)?;
        let resolved =
            fs::canonicalize(source).map_err(|err| io(source, "cannot resolve the source", err))?;
        if resolved.as_os_str() != source.as_os_str()
            || !fs::symlink_metadata(source)
                .map_err(|err| io(source, "cannot inspect the source", err))?
                .is_dir()
        {
            return Err(refuse(
                source,
                "expected a real folder at the canonical source",
            ));
        }
        let paths = LinkPaths {
            source: source.to_path_buf(),
            destination: destination.to_path_buf(),
            target: match request.style {
                TargetStyle::Absolute => source.to_path_buf(),
                TargetStyle::Relative => relative_between(parent, source)?,
            },
        };
        let leaf = paths.leaf()?;
        if probe(&namespace.public_folder()?, leaf, destination)?.is_some() {
            return Err(FsxError::new(
                FsxErrorKind::LinkExists,
                destination,
                "the destination is occupied",
            ));
        }
        let reference = ProtectedLinkRef {
            incarnation: namespace.incarnation(),
            secret: secret()?,
        };
        let journal = Journal::new(&namespace, &reference);
        journal.prepare(&paths)?;
        let pin = namespace.private_path(&journal.pin);
        rx::symlinkat(&paths.target, &namespace.private, &journal.pin).map_err(|err| {
            io(
                &pin,
                "cannot stage the acquisition after recording preparation",
                err,
            )
        })?;
        let ino = journal
            .observed_link(
                &namespace.private,
                OsStr::new(&journal.pin),
                &pin,
                &paths.target,
            )?
            .ok_or_else(|| refuse(&pin, "the staged acquisition is missing"))?;
        rx::fsync(&namespace.private)
            .map_err(|err| io(&pin, "cannot sync the private acquisition", err))?;
        let mut acquired = [b'A'; 9];
        acquired[1..].copy_from_slice(&ino.to_be_bytes());
        journal.append(&acquired)?;

        let folder = namespace.public_folder()?;
        // linkat without SYMLINK_FOLLOW publishes the acquired symlink itself and retains its private pin.
        rx::linkat(
            &namespace.private,
            &journal.pin,
            &folder,
            leaf,
            AtFlags::empty(),
        )
        .map_err(|err| {
            io(
                destination,
                "cannot publish the symlink; the private acquisition is retained",
                err,
            )
        })?;
        let unconfirmed = |err| {
            changed(
                err,
                destination,
                "the published entry has no confirmed reference",
            )
        };
        rx::fsync(&folder)
            .map_err(|err| unconfirmed(io(destination, "cannot sync publication", err)))?;
        if journal
            .observed_link(&folder, leaf, destination, &paths.target)
            .map_err(unconfirmed)?
            != Some(ino)
        {
            return Err(unconfirmed(refuse(
                destination,
                "published entry differs from the staged symlink",
            )));
        }
        drop(folder);
        let resolved = fs::canonicalize(destination).map_err(|err| {
            unconfirmed(io(destination, "cannot resolve the published symlink", err))
        })?;
        if resolved.as_os_str() != source.as_os_str() {
            return Err(unconfirmed(refuse(
                destination,
                "published symlink resolves outside its recorded source",
            )));
        }
        journal.append(b"P").map_err(unconfirmed)?;
        Ok(reference)
    }

    pub(super) fn remove(
        anchor: &Path,
        reference: &ProtectedLinkRef,
        stores: &[&Path],
    ) -> Result<Removal, FsxError> {
        let namespace = Namespace::open(anchor)?;
        if reference.incarnation != namespace.incarnation() {
            return Err(refuse(
                anchor,
                "the reference belongs to a different namespace incarnation",
            ));
        }
        let journal = Journal::new(&namespace, reference);
        let (paths, phase) = journal.load()?;
        let destination = &paths.destination;
        let parent = destination_parent(anchor, destination)?;
        let leaf = paths.leaf()?;
        if !canonical_absolute(&paths.source) || !source_in_stores(&paths.source, stores) {
            return Err(refuse(
                destination,
                "the recorded source is outside the known stores",
            ));
        }
        if paths.target.as_os_str() != paths.source.as_os_str()
            && paths.target.as_os_str() != relative_between(parent, &paths.source)?.as_os_str()
        {
            return Err(refuse(
                destination,
                "the recorded symlink target does not name the source",
            ));
        }
        let pin = namespace.private_path(&journal.pin);
        let folder = namespace.public_folder()?;
        let ino = match phase {
            Phase::Prepared | Phase::Acquired | Phase::Removing => {
                return Err(refuse(
                    destination,
                    "the acquisition or removal was interrupted; inspect the journal",
                ));
            }
            Phase::Retired => {
                if probe(&namespace.private, OsStr::new(&journal.pin), &pin)?.is_none()
                    && probe(&folder, leaf, destination)?.is_none()
                {
                    return Ok(Removal::AlreadyGone);
                }
                return Err(refuse(
                    destination,
                    "a retired reference cannot authorize deletion",
                ));
            }
            Phase::Active(ino) => ino,
        };
        if journal.observed_link(
            &namespace.private,
            OsStr::new(&journal.pin),
            &pin,
            &paths.target,
        )? != Some(ino)
        {
            return Err(refuse(
                &pin,
                "the private pin for the original acquisition is unproven",
            ));
        }
        match journal.observed_link(&folder, leaf, destination, &paths.target)? {
            None => return Ok(Removal::AlreadyGone),
            Some(found) if found == ino => {}
            Some(_) => {
                return Err(refuse(
                    destination,
                    "the destination is not the pinned acquisition",
                ));
            }
        }
        drop(folder);
        journal.append(b"D")?;
        let incomplete = |err| {
            changed(
                err,
                destination,
                "removal is incomplete; the journal requires inspection",
            )
        };
        let folder = namespace.public_folder().map_err(incomplete)?;
        rx::unlinkat(&folder, leaf, AtFlags::empty())
            .and_then(|()| rx::fsync(&folder))
            .map_err(|err| {
                incomplete(io(
                    destination,
                    "cannot unlink or sync the public symlink",
                    err,
                ))
            })?;
        drop(folder);
        rx::unlinkat(&namespace.private, &journal.pin, AtFlags::empty())
            .and_then(|()| rx::fsync(&namespace.private))
            .map_err(|err| incomplete(io(&pin, "cannot unlink or sync the private pin", err)))?;
        journal.append(b"X").map_err(incomplete)?;
        Ok(Removal::Removed)
    }
}
