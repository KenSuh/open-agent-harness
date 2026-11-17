// 관리 링크 시험: 기본 링크(macOS 심볼릭 링크·Windows 정션)와 보고되는 복사 대체, 남의 항목 보존, 링크 삭제가 대상을 지우지 않는지 확인한다.
use oah_core::fsx::FsxErrorKind;
use oah_core::fsx::links::{
    Entry, LinkKind, LinkRequest, NativeLinker, Removal, SystemLinker, TargetStyle, create,
    create_with, inspect, remove,
};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

struct Stores {
    _dir: tempfile::TempDir,
    store: PathBuf,
    agents: PathBuf,
}

impl Stores {
    fn new() -> Stores {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let store = root.join("store");
        let agents = root.join("agents");
        fs::create_dir_all(store.join("skill-a").join("scripts")).unwrap();
        fs::write(store.join("skill-a").join("SKILL.md"), "# Skill A\n").unwrap();
        fs::write(
            store.join("skill-a").join("scripts").join("run.sh"),
            "echo run\n",
        )
        .unwrap();
        fs::create_dir_all(&agents).unwrap();
        Stores {
            _dir: dir,
            store,
            agents,
        }
    }

    fn source(&self) -> PathBuf {
        self.store.join("skill-a")
    }

    fn request<'a>(
        &self,
        destination: &'a Path,
        style: TargetStyle,
        allow_copy: bool,
        source: &'a Path,
    ) -> LinkRequest<'a> {
        LinkRequest {
            source,
            destination,
            style,
            allow_copy,
        }
    }
}

/// A native linker that always fails, to drive the copy fallback deterministically.
struct Unavailable;

impl NativeLinker for Unavailable {
    fn kind(&self) -> LinkKind {
        LinkKind::Symlink
    }

    fn link(&self, _source: &Path, _destination: &Path, _target: &Path) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "links are switched off here",
        ))
    }
}

struct Misdirected {
    elsewhere: PathBuf,
}

impl NativeLinker for Misdirected {
    fn kind(&self) -> LinkKind {
        SystemLinker.kind()
    }

    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()> {
        SystemLinker.link(source, destination, target)?;
        #[cfg(windows)]
        fs::remove_dir(destination)?;
        #[cfg(not(windows))]
        fs::remove_file(destination)?;
        SystemLinker.link(&self.elsewhere, destination, &self.elsewhere)
    }
}

fn native_kind() -> LinkKind {
    if cfg!(windows) {
        LinkKind::Junction
    } else {
        LinkKind::Symlink
    }
}

#[test]
fn a_native_link_points_at_the_store_and_is_reported_once() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let request = stores.request(&destination, TargetStyle::Absolute, false, &source);
    let outcome = create(&request).unwrap();
    assert!(outcome.created);
    assert!(outcome.fallback.is_none());
    assert_eq!(outcome.link.kind, native_kind());
    assert_eq!(outcome.link.target.as_deref(), Some(source.as_path()));
    match inspect(&destination).unwrap() {
        Entry::Symlink { target } | Entry::Junction { target } => {
            assert_eq!(fs::canonicalize(target).unwrap(), source);
        }
        other => panic!("not a link: {other:?}"),
    }
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(
        create(&request).unwrap_err().kind(),
        FsxErrorKind::LinkExists
    );
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[cfg(unix)]
#[test]
fn a_project_link_stores_a_relative_target() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Relative, false, &source)).unwrap();
    let target = outcome.link.target.clone().unwrap();
    assert!(target.is_relative(), "{target:?}");
    assert_eq!(fs::read_link(&destination).unwrap(), target);
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
}

#[test]
fn an_occupied_destination_is_left_alone() {
    let stores = Stores::new();
    let source = stores.source();
    let folder = stores.agents.join("mine");
    fs::create_dir(&folder).unwrap();
    fs::write(folder.join("notes.md"), "user text").unwrap();
    let error = create(&stores.request(&folder, TargetStyle::Absolute, true, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkExists);
    assert_eq!(
        fs::read_to_string(folder.join("notes.md")).unwrap(),
        "user text"
    );
    let file = stores.agents.join("file");
    fs::write(&file, "kept").unwrap();
    let error = create(&stores.request(&file, TargetStyle::Absolute, true, &source)).unwrap_err();
    assert_eq!(error.code(), "OAH_LINK_EXISTS");
    assert_eq!(fs::read_to_string(&file).unwrap(), "kept");
}

#[test]
fn removing_a_link_never_removes_its_target() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap(),
        Removal::Removed
    );
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    assert_eq!(
        fs::read_to_string(source.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert!(source.join("scripts").join("run.sh").is_file());
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap(),
        Removal::AlreadyGone
    );
}

#[test]
fn a_link_that_is_no_longer_ours_is_not_removed() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap();

    let elsewhere = stores.agents.join("unrelated-store");
    let error = remove(&outcome.link, &[&elsewhere]).unwrap_err();
    assert_eq!(
        error.kind(),
        FsxErrorKind::NotOwned,
        "the source is outside every store"
    );
    assert!(matches!(
        inspect(&destination).unwrap(),
        Entry::Symlink { .. } | Entry::Junction { .. }
    ));

    let other = stores.store.join("skill-b");
    fs::create_dir(&other).unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap(),
        Removal::Removed
    );
    let replaced =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &other)).unwrap();
    let error = remove(&outcome.link, &[&stores.store]).unwrap_err();
    assert_eq!(
        error.code(),
        "OAH_LINK_NOT_OWNED",
        "the link now points somewhere else"
    );
    assert_eq!(
        remove(&replaced.link, &[&stores.store]).unwrap(),
        Removal::Removed
    );
}

#[test]
fn a_failed_native_link_falls_back_to_a_reported_copy() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Copy);
    let reason = outcome
        .fallback
        .expect("a copy reports why the native link was not made");
    assert!(
        reason.contains("unavailable") && reason.contains("links are switched off here"),
        "{reason}"
    );
    assert!(outcome.link.target.is_none());
    assert_eq!(inspect(&destination).unwrap(), Entry::Directory);
    let files: Vec<&Path> = outcome
        .link
        .files
        .iter()
        .map(|file| file.relative.as_path())
        .collect();
    assert_eq!(
        files,
        [Path::new("SKILL.md"), &Path::new("scripts").join("run.sh")]
    );
    assert_eq!(outcome.link.folders, [PathBuf::from("scripts")]);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );

    fs::write(source.join("SKILL.md"), "# Changed\n").unwrap();
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n",
        "a copy does not follow the source"
    );
}

#[test]
fn without_permission_to_copy_a_failed_link_is_an_error() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let error = create_with(
        &stores.request(&destination, TargetStyle::Absolute, false, &source),
        &Unavailable,
    )
    .unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
}

#[test]
fn cleanup_ownership_native_replacement_survives_with_or_without_copy() {
    let stores = Stores::new();
    let source = stores.source();
    let elsewhere = stores.store.join("elsewhere");
    fs::create_dir_all(&elsewhere).unwrap();
    fs::write(elsewhere.join("keep.txt"), "kept\n").unwrap();
    let linker = Misdirected {
        elsewhere: elsewhere.clone(),
    };
    for allow_copy in [false, true] {
        let destination = stores.agents.join(format!("skill-{allow_copy}"));
        let error = create_with(
            &stores.request(&destination, TargetStyle::Absolute, allow_copy, &source),
            &linker,
        )
        .unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::NotOwned);
        assert!(matches!(
            inspect(&destination).unwrap(),
            Entry::Symlink { .. } | Entry::Junction { .. }
        ));
        assert_eq!(fs::canonicalize(&destination).unwrap(), elsewhere);
    }
    assert_eq!(
        fs::read_to_string(elsewhere.join("keep.txt")).unwrap(),
        "kept\n",
        "removing the wrong link touched its target"
    );
}

#[test]
fn unowned_matching_link_cannot_be_adopted_and_removed() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("user-link");
    SystemLinker.link(&source, &destination, &source).unwrap();
    let error =
        create(&stores.request(&destination, TargetStyle::Absolute, true, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkExists);
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[test]
fn cleanup_ownership_descriptor_rejects_even_a_matching_replacement() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let owned =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap();
    let saved = stores.agents.join("original-link");
    fs::rename(&destination, &saved).unwrap();
    SystemLinker.link(&source, &destination, &source).unwrap();
    assert_eq!(
        remove(&owned.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[test]
fn a_copy_is_removed_only_while_it_matches_its_record() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap();
    fs::write(destination.join("SKILL.md"), "# Edited by the user\n").unwrap();
    let error = remove(&outcome.link, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Edited by the user\n"
    );

    fs::write(destination.join("SKILL.md"), "# Skill A\n").unwrap();
    fs::write(destination.join("added.txt"), "new").unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );

    fs::remove_file(destination.join("added.txt")).unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap(),
        Removal::Removed
    );
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    assert!(
        source.join("SKILL.md").is_file(),
        "removing a copy left the source alone"
    );
}

#[cfg(unix)]
#[test]
fn copying_never_follows_links() {
    let stores = Stores::new();
    let source = stores.source();
    let outside = stores.agents.join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("private.txt"), "not part of the skill").unwrap();
    std::os::unix::fs::symlink(&outside, source.join("escape")).unwrap();
    let destination = stores.agents.join("skill-a");
    let error = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::TreeHasLink);
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    let leftovers: Vec<_> = fs::read_dir(&stores.agents)
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(
        leftovers,
        ["outside"],
        "the staging copy was not cleaned up"
    );
}

#[cfg(unix)]
#[test]
fn a_copy_keeps_the_executable_bit() {
    use std::os::unix::fs::PermissionsExt;
    let stores = Stores::new();
    let source = stores.source();
    let script = source.join("scripts").join("run.sh");
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    let destination = stores.agents.join("skill-a");
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap();
    let recorded = outcome
        .link
        .files
        .iter()
        .find(|file| file.relative.ends_with("run.sh"))
        .unwrap();
    assert!(recorded.executable);
    let mode = fs::metadata(destination.join("scripts").join("run.sh"))
        .unwrap()
        .permissions()
        .mode();
    assert_ne!(mode & 0o111, 0);
}

#[cfg(windows)]
#[test]
fn a_real_junction_is_made_without_elevation() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Relative, false, &source)).unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Junction);
    assert_eq!(
        outcome.link.target.as_deref(),
        Some(source.as_path()),
        "junctions store an absolute target"
    );
    assert!(junction::exists(&destination).unwrap());
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap(),
        Removal::Removed
    );
    assert!(source.join("SKILL.md").is_file());
}

#[cfg(windows)]
fn require_windows_context(guard: u32) {
    let script = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class LinkTestContext {
  [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessMitigationPolicy(IntPtr process, int policy, out uint flags, UIntPtr size);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer, int size, out int needed);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  public static string Read(int pid) {
    IntPtr process = OpenProcess(0x1000, false, pid);
    if (process == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
    IntPtr token = IntPtr.Zero;
    IntPtr label = IntPtr.Zero;
    IntPtr elevation = IntPtr.Zero;
    try {
      uint flags;
      if (!GetProcessMitigationPolicy(process, 16, out flags, (UIntPtr)4)) throw new System.ComponentModel.Win32Exception();
      if (!OpenProcessToken(process, 8, out token)) throw new System.ComponentModel.Win32Exception();
      int needed;
      GetTokenInformation(token, 25, IntPtr.Zero, 0, out needed);
      label = Marshal.AllocHGlobal(needed);
      if (!GetTokenInformation(token, 25, label, needed, out needed)) throw new System.ComponentModel.Win32Exception();
      string sid = new SecurityIdentifier(Marshal.ReadIntPtr(label)).Value;
      elevation = Marshal.AllocHGlobal(4);
      if (!GetTokenInformation(token, 20, elevation, 4, out needed)) throw new System.ComponentModel.Win32Exception();
      return "integrity=" + sid + " elevated=" + Marshal.ReadInt32(elevation) + " redirection_guard=" + (flags & 1);
    } finally {
      if (label != IntPtr.Zero) Marshal.FreeHGlobal(label);
      if (elevation != IntPtr.Zero) Marshal.FreeHGlobal(elevation);
      if (token != IntPtr.Zero) CloseHandle(token);
      CloseHandle(process);
    }
  }
}
'@
[LinkTestContext]::Read(PARENT_PID)
"#.replace("PARENT_PID", &std::process::id().to_string());
    let output = std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let context = String::from_utf8(output.stdout).unwrap();
    assert_eq!(
        context.trim(),
        format!("integrity=S-1-16-8192 elevated=0 redirection_guard={guard}")
    );
    println!(
        "WINDOWS_CONTEXT pid={} {}",
        std::process::id(),
        context.trim()
    );
}

#[cfg(windows)]
#[test]
#[ignore = "requires ordinary medium integrity with Redirection Guard disabled; run by the native evidence gate"]
fn ordinary_medium_integrity_native_junction_contract() {
    require_windows_context(0);
    a_real_junction_is_made_without_elevation();
}

#[cfg(windows)]
#[test]
#[ignore = "requires medium integrity and actual Redirection Guard traversal refusal; run by the native evidence gate"]
fn system_linker_copy_under_actual_traversal_refusal() {
    require_windows_context(1);
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let error =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
    assert_eq!(
        error.io_error().and_then(io::Error::raw_os_error),
        Some(448)
    );
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, true, &source)).unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Copy);
    assert!(
        outcome
            .fallback
            .as_deref()
            .unwrap()
            .contains("os error 448")
    );
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap(),
        Removal::Removed
    );
    assert_eq!(
        fs::read_to_string(source.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
}
