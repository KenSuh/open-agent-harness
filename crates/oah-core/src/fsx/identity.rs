// 항목 신원: 링크를 따라가지 않고 열린 핸들을 보관해, 임시 항목의 교체와 덮어쓰기를 거부한다.
use std::fs::File;
use std::io;
use std::path::Path;
use std::sync::Arc;

pub(super) type Identity = Arc<same_file::Handle>;

pub(super) fn from_file(file: File) -> io::Result<Identity> {
    same_file::Handle::from_file(file).map(Arc::new)
}

#[cfg(unix)]
pub(super) fn capture(path: &Path) -> io::Result<Identity> {
    use rustix::fs::{Mode, OFlags, open};
    #[cfg(target_os = "linux")]
    let flags = if std::fs::symlink_metadata(path)?.file_type().is_symlink() {
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC
    } else {
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC
    };
    #[cfg(target_vendor = "apple")]
    let flags = if std::fs::symlink_metadata(path)?.file_type().is_symlink() {
        OFlags::SYMLINK | OFlags::CLOEXEC
    } else {
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC
    };
    #[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let file: File = open(path, flags, Mode::empty())?.into();
    from_file(file)
}

#[cfg(windows)]
pub(super) fn capture(path: &Path) -> io::Result<Identity> {
    use std::os::windows::fs::OpenOptionsExt;
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x02000000;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x00200000;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)?;
    from_file(file)
}

#[cfg(not(any(unix, windows)))]
pub(super) fn capture(_path: &Path) -> io::Result<Identity> {
    Err(io::ErrorKind::Unsupported.into())
}

pub(super) fn matches(path: &Path, identity: &Identity) -> bool {
    capture(path).is_ok_and(|current| current == *identity)
}

pub(super) fn rename_new(from: &Path, to: &Path) -> io::Result<()> {
    #[cfg(any(target_os = "linux", target_vendor = "apple"))]
    {
        use rustix::fs::{CWD, RenameFlags, renameat_with};
        renameat_with(CWD, from, CWD, to, RenameFlags::NOREPLACE)?;
        Ok(())
    }
    #[cfg(windows)]
    {
        // Windows refuses to replace an existing directory in a directory rename.
        std::fs::rename(from, to)
    }
    #[cfg(not(any(target_os = "linux", target_vendor = "apple", windows)))]
    {
        let _ = (from, to);
        Err(io::ErrorKind::Unsupported.into())
    }
}
