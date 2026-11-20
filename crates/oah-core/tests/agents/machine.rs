// 가짜 기계: 임시 폴더에 가짜 홈·PATH 폴더·가짜 실행 파일을 두고, 실제 확인 통로로 확인하되 묶음 검사는 임시 폴더 안만 보게 한다.
use oah_core::agents::{Os, Probe, Resolution, Resolved, RunError, RunOutput, SystemProbe};
use oah_core::app::{self, Session};
use oah_core::dto::{AgentStatusDto, AgentsProbeReq};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// A temporary root with `home` (the home folder) and `bin` (the only `PATH` folder). Agent programs run with
/// this environment alone, so a script names every tool it uses by its full path.
pub(crate) struct Machine {
    _dir: tempfile::TempDir,
    pub(crate) root: PathBuf,
    pub(crate) home: PathBuf,
    pub(crate) bin: PathBuf,
}

impl Machine {
    pub(crate) fn new() -> Machine {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let (home, bin) = (root.join("home"), root.join("bin"));
        fs::create_dir_all(&home).unwrap();
        fs::create_dir_all(&bin).unwrap();
        Machine {
            _dir: dir,
            root,
            home,
            bin,
        }
    }

    pub(crate) fn session(&self) -> Session {
        Session::new(
            Env::new(&self.root)
                .unwrap()
                .with("HOME", &self.home)
                .with("PATH", &self.bin),
        )
    }

    pub(crate) fn script(&self, folder: &Path, name: &str, body: &str) {
        let path = folder.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// A program that records that it ran in `<root>/ran-<name>`.
    pub(crate) fn tripwire(&self, folder: &Path, name: &str) -> PathBuf {
        let marker = self.root.join(format!("ran-{name}"));
        self.script(
            folder,
            name,
            &format!("/usr/bin/touch '{}'\n", marker.display()),
        );
        marker
    }
}

/// The real probe, with bundle checks kept inside the temporary root so the machine's own applications are never
/// consulted.
pub(crate) struct Hermetic<'a> {
    system: SystemProbe<'a>,
    root: &'a Path,
}

impl Probe for Hermetic<'_> {
    fn env(&self) -> &Env {
        self.system.env()
    }
    fn os(&self) -> Os {
        self.system.os()
    }
    fn resolve(&self, command: &str) -> Resolution {
        self.system.resolve(command)
    }
    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        self.system.resolve_in(command, folder)
    }
    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        deadline: Duration,
    ) -> Result<RunOutput, RunError> {
        self.system.run(program, args, deadline)
    }
    fn exists(&self, path: &Path) -> bool {
        path.starts_with(self.root) && self.system.exists(path)
    }
}

pub(crate) fn probe(
    machine: &Machine,
    session: &Session,
    req: AgentsProbeReq,
) -> Vec<AgentStatusDto> {
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let probe = Hermetic {
        system: SystemProbe::new(ctx.env),
        root: &machine.root,
    };
    app::agents_probe_with(&ctx, req, &probe).unwrap()
}

/// Every path below `root` with its kind, mode and bytes (a file's content, a link's target).
pub(crate) fn tree(root: &Path) -> BTreeMap<PathBuf, String> {
    let (mut entries, mut folders) = (BTreeMap::new(), vec![root.to_path_buf()]);
    while let Some(folder) = folders.pop() {
        for entry in fs::read_dir(&folder).unwrap() {
            let path = entry.unwrap().path();
            let meta = fs::symlink_metadata(&path).unwrap();
            let mode = meta.permissions().mode();
            let value = if meta.file_type().is_symlink() {
                format!("link {}", fs::read_link(&path).unwrap().display())
            } else if meta.is_dir() {
                folders.push(path.clone());
                format!("folder {mode:o}")
            } else {
                format!("file {mode:o} {:?}", fs::read(&path).unwrap())
            };
            entries.insert(path.strip_prefix(root).unwrap().to_path_buf(), value);
        }
    }
    entries
}
