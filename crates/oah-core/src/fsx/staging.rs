// 복사 임시 영역: 배타적으로 만든 항목의 열린 신원만 기록하고, 실패 시 그 항목만 역순으로 지운다.
use super::error::{FsxError, FsxErrorKind};
use super::identity::{self, Identity};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

pub(super) struct CopyStage {
    path: PathBuf,
    pub identity: Identity,
    entries: Vec<(PathBuf, Identity, bool)>,
}

impl CopyStage {
    pub fn acquire(path: &Path) -> Result<Self, FsxError> {
        fs::create_dir(path)
            .map_err(|error| io_error(path, "cannot exclusively acquire copy staging", error))?;
        let identity = identity::capture(path)
            .map_err(|error| io_error(path, "cannot retain copy staging identity", error))?;
        Ok(Self {
            path: path.to_path_buf(),
            identity,
            entries: Vec::new(),
        })
    }

    fn check(&self) -> Result<(), FsxError> {
        if identity::matches(&self.path, &self.identity) {
            Ok(())
        } else {
            Err(not_owned(&self.path))
        }
    }

    pub fn directory(&mut self, relative: &Path) -> Result<(), FsxError> {
        self.check()?;
        let path = self.path.join(relative);
        fs::create_dir(&path)
            .map_err(|error| io_error(&path, "cannot exclusively create a copied folder", error))?;
        let identity = identity::capture(&path)
            .map_err(|error| io_error(&path, "cannot retain copied folder identity", error))?;
        self.entries.push((path, identity, true));
        Ok(())
    }

    pub fn file(&mut self, source: &Path, relative: &Path) -> Result<(), FsxError> {
        self.check()?;
        let input = identity::capture(source).map_err(|error| {
            io_error(
                source,
                "cannot open copy source without following links",
                error,
            )
        })?;
        let metadata = input
            .as_file()
            .metadata()
            .map_err(|error| io_error(source, "cannot inspect copy source", error))?;
        if !metadata.is_file() {
            return Err(FsxError::new(
                FsxErrorKind::TreeHasLink,
                source,
                "the copy source is no longer a regular file",
            ));
        }
        let path = self.path.join(relative);
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| io_error(&path, "cannot exclusively create a copied file", error))?;
        let held = output
            .try_clone()
            .and_then(identity::from_file)
            .map_err(|error| io_error(&path, "cannot retain copied file identity", error))?;
        self.entries.push((path.clone(), held, false));
        io::copy(&mut input.as_file(), &mut output)
            .and_then(|_| output.set_permissions(metadata.permissions()))
            .map_err(|error| io_error(&path, "cannot copy", error))?;
        Ok(())
    }

    pub fn publish(&self, destination: &Path) -> io::Result<()> {
        self.check().map_err(io::Error::other)?;
        identity::rename_new(&self.path, destination)
    }

    pub fn cleanup(&self) -> Result<(), FsxError> {
        self.check()?;
        for (path, identity, _) in &self.entries {
            if !identity::matches(path, identity) {
                return Err(not_owned(path));
            }
        }
        for (path, identity, directory) in self.entries.iter().rev() {
            self.check()?;
            if !identity::matches(path, identity) {
                return Err(not_owned(path));
            }
            let removed = if *directory {
                fs::remove_dir(path)
            } else {
                fs::remove_file(path)
            };
            removed.map_err(|error| io_error(path, "cannot clean an owned copied entry", error))?;
        }
        self.check()?;
        fs::remove_dir(&self.path)
            .map_err(|error| io_error(&self.path, "cannot clean owned copy staging", error))
    }
}

fn io_error(path: &Path, action: &str, error: io::Error) -> FsxError {
    FsxError::io(FsxErrorKind::Io, path, action, error)
}

fn not_owned(path: &Path) -> FsxError {
    FsxError::new(
        FsxErrorKind::NotOwned,
        path,
        "copy staging was replaced; it was left alone",
    )
}
