// 등록된 Linux 네임스페이스의 링크·복사 작업에 필요한 파일 시스템 권한을 확인한다.
//! Staging authority in an enrolled Linux namespace.

#[cfg(target_os = "linux")]
pub(super) mod protected {
    use crate::fsx::{Fingerprint, FsxError, FsxErrorKind};
    use rustix::fd::{AsFd, OwnedFd};
    use rustix::fs::{self as rx, AtFlags, FileType, Mode, OFlags, Statx, StatxFlags};
    use rustix::io::Errno;
    use std::ffi::OsStr;
    use std::fs::File;
    use std::io::Read;
    use std::os::unix::ffi::OsStrExt;
    use std::path::{Path, PathBuf};

    const DIRECTORY: OFlags = OFlags::RDONLY
        .union(OFlags::DIRECTORY)
        .union(OFlags::CLOEXEC)
        .union(OFlags::NOFOLLOW);
    pub(in crate::fsx) const FILE_FLAGS: OFlags = OFlags::CLOEXEC
        .union(OFlags::NOFOLLOW)
        .union(OFlags::NONBLOCK);
    const ENROLLMENT: &str = ".oah-enrollment";
    const PRIVATE: &str = ".oah-links";
    pub(in crate::fsx) const PUBLIC: &str = "links";

    #[derive(PartialEq, Eq)]
    struct Binding(u32, u32, u64, i64, u32);

    impl Binding {
        fn parse(line: &str) -> Option<Self> {
            let mut fields = line.split_ascii_whitespace();
            let binding = Self(
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
            );
            fields.next().is_none().then_some(binding)
        }

        fn matches(&self, stat: &Statx) -> bool {
            StatxFlags::from_bits_retain(stat.stx_mask).contains(StatxFlags::BTIME)
                && *self
                    == Self(
                        stat.stx_dev_major,
                        stat.stx_dev_minor,
                        stat.stx_ino,
                        stat.stx_btime.tv_sec,
                        stat.stx_btime.tv_nsec,
                    )
        }
    }

    struct Enrollment {
        writer: (u32, u32),
        incarnation: Fingerprint,
        anchor: Binding,
        public: Binding,
        private: Binding,
    }

    impl Enrollment {
        fn parse(text: &str) -> Option<Self> {
            let mut lines = text.lines();
            if lines.next()? != "oah-protected-links-v1" {
                return None;
            }
            let (uid, gid) = lines.next()?.split_once(' ')?;
            let spec = Self {
                writer: (uid.parse().ok()?, gid.parse().ok()?),
                incarnation: Fingerprint::from_hex(lines.next()?)?,
                anchor: Binding::parse(lines.next()?)?,
                public: Binding::parse(lines.next()?)?,
                private: Binding::parse(lines.next()?)?,
            };
            (spec.writer.0 != 0 && spec.writer.1 != 0 && lines.next().is_none()).then_some(spec)
        }
    }

    // One private descriptor anchors the lock; the second slot is reused for public directory or journal I/O.
    pub(in crate::fsx) struct Namespace<'a> {
        pub anchor: &'a Path,
        pub private: OwnedFd,
        pub mount: u64,
        enrollment: Enrollment,
    }

    impl<'a> Namespace<'a> {
        pub fn open(anchor: &'a Path) -> Result<Self, FsxError> {
            let directory = open_anchor(anchor)?;
            let stat = descriptor(&directory, anchor)?;
            if !is_ext4_mount(stat.stx_mnt_id)? {
                return Err(refuse(anchor, "the enrolled anchor must be on local ext4"));
            }
            let path = anchor.join(ENROLLMENT);
            let fd = rx::openat(
                &directory,
                ENROLLMENT,
                OFlags::RDONLY | FILE_FLAGS,
                Mode::empty(),
            )
            .map_err(|err| authority_open_error(&path, err))?;
            let manifest = descriptor(&fd, &path)?;
            if (manifest.stx_uid, manifest.stx_gid) != (0, 0)
                || manifest.stx_mode != 0o100644
                || manifest.stx_nlink != 1
                || manifest.stx_mnt_id != stat.stx_mnt_id
            {
                return Err(refuse(
                    &path,
                    "unexpected enrollment owner, mode, type or mount",
                ));
            }
            mode_only(&fd, &path, false)?;
            let enrollment = Enrollment::parse(&read_text(fd, &path, 1024)?)
                .ok_or_else(|| refuse(&path, "unrecognized enrollment"))?;
            if !enrollment.anchor.matches(&stat) {
                return Err(refuse(anchor, "enrollment does not bind the opened anchor"));
            }
            if !writer_credentials(enrollment.writer)? {
                return Err(refuse(
                    anchor,
                    "this thread is not the enrolled unprivileged writer",
                ));
            }
            let path = anchor.join(PRIVATE);
            let private = rx::openat(&directory, PRIVATE, DIRECTORY, Mode::empty())
                .map_err(|err| authority_open_error(&path, err))?;
            drop(directory);
            let ns = Self {
                anchor,
                private,
                mount: stat.stx_mnt_id,
                enrollment,
            };
            ns.verify_directory(&ns.private, &path, 0o700, &ns.enrollment.private)?;
            drop(ns.public_folder()?);
            match rx::flock(&ns.private, rx::FlockOperation::NonBlockingLockExclusive) {
                Ok(()) => Ok(ns),
                Err(Errno::WOULDBLOCK) => Err(FsxError::io(
                    FsxErrorKind::Busy,
                    &path,
                    "the enrolled writer lock is busy",
                    Errno::WOULDBLOCK.into(),
                )),
                Err(err) => Err(io(&path, "cannot lock the enrolled journal directory", err)),
            }
        }

        pub fn public_folder(&self) -> Result<OwnedFd, FsxError> {
            let path = self.anchor.join(PUBLIC);
            // The enrolled directories are siblings whose names can only be changed by the provisioner.
            let fd = rx::openat(&self.private, "../links", DIRECTORY, Mode::empty())
                .map_err(|err| authority_open_error(&path, err))?;
            self.verify_directory(&fd, &path, 0o755, &self.enrollment.public)?;
            Ok(fd)
        }

        pub fn private_path(&self, name: &str) -> PathBuf {
            self.anchor.join(PRIVATE).join(name)
        }

        pub fn writer(&self) -> (u32, u32) {
            self.enrollment.writer
        }

        pub fn incarnation(&self) -> Fingerprint {
            self.enrollment.incarnation
        }

        fn verify_directory(
            &self,
            fd: &OwnedFd,
            path: &Path,
            mode: u16,
            binding: &Binding,
        ) -> Result<(), FsxError> {
            let stat = descriptor(fd, path)?;
            if (stat.stx_uid, stat.stx_gid) != self.writer()
                || stat.stx_mode != 0o040000 | mode
                || stat.stx_mnt_id != self.mount
                || !binding.matches(&stat)
            {
                return Err(refuse(
                    path,
                    "the writer directory does not match its enrollment",
                ));
            }
            mode_only(fd, path, true)
        }
    }

    pub(in crate::fsx) fn canonical_absolute(path: &Path) -> bool {
        let raw = path.as_os_str().as_bytes();
        raw == b"/"
            || (raw.len() < 4096
                && raw.strip_prefix(b"/").is_some_and(|tail| {
                    tail.split(|&byte| byte == b'/')
                        .all(|name| !matches!(name, b"" | b"." | b"..") && !name.contains(&0))
                }))
    }

    fn open_anchor(anchor: &Path) -> Result<OwnedFd, FsxError> {
        if !canonical_absolute(anchor) || anchor.parent().is_none() {
            return Err(refuse(
                anchor,
                "the enrollment anchor must be a canonical non-root path",
            ));
        }
        let mut path = PathBuf::from("/");
        let mut directory = rx::open(&path, DIRECTORY, Mode::empty())
            .map_err(|err| authority_open_error(&path, err))?;
        let root = verify_ancestor(&directory, &path)?;
        let root_type = rx::fstatfs(&directory)
            .map_err(|err| io(&path, "cannot inspect the root filesystem", err))?
            .f_type;
        if !matches!(root_type, 0x794c7630 | 0xef53) {
            return Err(refuse(
                &path,
                "expected an overlay or ext-family root mount",
            ));
        }
        for component in anchor.components().skip(1) {
            let name = component.as_os_str();
            path.push(name);
            let next = rx::openat(&directory, name, DIRECTORY, Mode::empty())
                .map_err(|err| authority_open_error(&path, err))?;
            let stat = verify_ancestor(&next, &path)?;
            if path != anchor && stat.stx_mnt_id != root.stx_mnt_id {
                return Err(refuse(
                    &path,
                    "only the enrollment anchor may cross a mount boundary",
                ));
            }
            directory = next;
        }
        Ok(directory)
    }

    fn verify_ancestor(fd: &OwnedFd, path: &Path) -> Result<Statx, FsxError> {
        let stat = descriptor(fd, path)?;
        if (stat.stx_uid, stat.stx_gid, stat.stx_mode) != (0, 0, 0o040755) {
            return Err(refuse(
                path,
                "the enrollment path requires a root-owned 0755 directory",
            ));
        }
        mode_only(fd, path, true)?;
        Ok(stat)
    }

    pub(in crate::fsx) fn mode_only(
        fd: &OwnedFd,
        path: &Path,
        directory: bool,
    ) -> Result<(), FsxError> {
        for name in ["system.posix_acl_access", "system.posix_acl_default"]
            .into_iter()
            .take(if directory { 2 } else { 1 })
        {
            match rx::fgetxattr_query(fd, name) {
                Err(Errno::NODATA | Errno::NOTSUP) => {}
                Ok(_) => return Err(refuse(path, "an extended POSIX ACL is unsupported")),
                Err(err) => return Err(io(path, "cannot inspect POSIX ACL", err)),
            }
        }
        Ok(())
    }

    fn is_ext4_mount(id: u64) -> Result<bool, FsxError> {
        let info = proc_text("/proc/thread-self/mountinfo", 1 << 20)?;
        let id = id.to_string();
        Ok(info
            .lines()
            .filter_map(|line| line.split_once(" - "))
            .any(|(mount, fs)| {
                mount.split_ascii_whitespace().next() == Some(&id)
                    && fs.split_ascii_whitespace().next() == Some("ext4")
            }))
    }

    fn writer_credentials((uid, gid): (u32, u32)) -> Result<bool, FsxError> {
        for file in ["/proc/thread-self/uid_map", "/proc/thread-self/gid_map"] {
            if !proc_text(file, 256)?
                .split_ascii_whitespace()
                .eq(["0", "0", "4294967295"])
            {
                return Ok(false);
            }
        }
        let status = proc_text("/proc/thread-self/status", 1 << 16)?;
        let field = |key| {
            status.lines().find_map(|line| {
                let (name, value) = line.split_once(':')?;
                (name == key).then(|| value.trim())
            })
        };
        let matches_id = |key, id: u32| {
            field(key).is_some_and(|value| {
                value
                    .split_ascii_whitespace()
                    .map(str::parse::<u32>)
                    .eq(std::iter::repeat_n(Ok(id), 4))
            })
        };
        Ok(matches_id("Uid", uid)
            && matches_id("Gid", gid)
            && field("NoNewPrivs") == Some("1")
            && ["CapInh", "CapPrm", "CapEff", "CapAmb"]
                .into_iter()
                .all(|key| field(key) == Some("0000000000000000")))
    }

    fn proc_text(file: &str, limit: u64) -> Result<String, FsxError> {
        let path = Path::new(file);
        let fd = rx::open(path, OFlags::RDONLY | FILE_FLAGS, Mode::empty())
            .map_err(|err| authority_open_error(path, err))?;
        if rx::fstatfs(&fd)
            .map_err(|err| io(path, "cannot inspect procfs", err))?
            .f_type
            != rx::PROC_SUPER_MAGIC
            || FileType::from_raw_mode(u32::from(descriptor(&fd, path)?.stx_mode))
                != FileType::RegularFile
        {
            return Err(refuse(path, "expected a kernel procfs record"));
        }
        read_text(fd, path, limit)
    }

    fn read_text(fd: OwnedFd, path: &Path, limit: u64) -> Result<String, FsxError> {
        let mut bytes = Vec::new();
        File::from(fd)
            .take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|err| io(path, "cannot read the authority record", err))?;
        if bytes.len() as u64 > limit {
            return Err(refuse(path, "the authority record is too large"));
        }
        String::from_utf8(bytes).map_err(|_| refuse(path, "the authority record is not UTF-8"))
    }

    pub(in crate::fsx) fn descriptor(fd: &impl AsFd, path: &Path) -> Result<Statx, FsxError> {
        probe(fd, OsStr::new(""), path)?.ok_or_else(|| refuse(path, "the opened entry is missing"))
    }

    pub(in crate::fsx) fn probe(
        fd: &impl AsFd,
        name: &OsStr,
        display: &Path,
    ) -> Result<Option<Statx>, FsxError> {
        let required = StatxFlags::BASIC_STATS | StatxFlags::MNT_ID;
        let stat = rx::statx(
            fd,
            name,
            AtFlags::EMPTY_PATH | AtFlags::SYMLINK_NOFOLLOW | AtFlags::NO_AUTOMOUNT,
            required | StatxFlags::BTIME,
        );
        match stat {
            Ok(stat) if StatxFlags::from_bits_retain(stat.stx_mask).contains(required) => {
                Ok(Some(stat))
            }
            Ok(_) | Err(Errno::NOSYS) => {
                Err(refuse(display, "mount and entry metadata are unavailable"))
            }
            Err(Errno::NOENT) => Ok(None),
            Err(err) => Err(io(display, "cannot inspect the anchored entry", err)),
        }
    }

    fn authority_open_error(path: &Path, err: Errno) -> FsxError {
        match err {
            Errno::NOENT | Errno::NOTDIR | Errno::LOOP => refuse(
                path,
                "an enrolled component is missing or not a plain entry",
            ),
            _ => io(path, "cannot open the enrolled component", err),
        }
    }

    pub(in crate::fsx) fn refuse(path: &Path, why: &str) -> FsxError {
        FsxError::new(FsxErrorKind::NotOwned, path, why)
    }

    pub(in crate::fsx) fn io(
        path: &Path,
        action: &str,
        err: impl Into<std::io::Error>,
    ) -> FsxError {
        FsxError::io(FsxErrorKind::Io, path, action, err.into())
    }
}
