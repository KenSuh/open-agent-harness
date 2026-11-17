// Windows 항목을 따라가지 않는 핸들, 비대체 이동과 FAT 디렉터리 할당 증거를 제공한다.
use std::ffi::OsString;
use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, Prefix};

/// No `MOVEFILE_REPLACE_EXISTING`: an existing target, file or folder, is refused.
const NO_REPLACE: u32 = 0;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32;
    fn GetVolumeInformationByHandleW(
        file: *mut c_void,
        volume_name: *mut u16,
        volume_size: u32,
        serial: *mut u32,
        component_length: *mut u32,
        flags: *mut u32,
        filesystem: *mut u16,
        filesystem_size: u32,
    ) -> i32;
    fn DeviceIoControl(
        file: *mut c_void,
        code: u32,
        input: *const c_void,
        input_size: u32,
        output: *mut c_void,
        output_size: u32,
        returned: *mut u32,
        overlapped: *mut c_void,
    ) -> i32;
}

pub(super) fn open(path: &Path) -> io::Result<File> {
    OpenOptions::new()
        .access_mode(0)
        .share_mode(7)
        .custom_flags(0x0200_0000 | 0x0020_0000)
        .open(path)
}

#[repr(C)]
#[derive(Default)]
struct Retrieval {
    count: u32,
    starting: i64,
    next: i64,
    lcn: i64,
}

const _: () = assert!(std::mem::size_of::<Retrieval>() == 32);

pub(super) fn directory_cluster(file: &File, attributes: u64) -> io::Result<Option<u64>> {
    if attributes & 0x10 == 0 {
        return Ok(None);
    }
    let mut filesystem = [0_u16; 32];
    // SAFETY: live synchronous handle; the initialized UTF-16 output holds 32 code units. Other outputs are optional null pointers.
    let result = unsafe {
        GetVolumeInformationByHandleW(
            file.as_raw_handle(),
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            filesystem.as_mut_ptr(),
            32,
        )
    };
    if result == 0 {
        return Err(io::Error::last_os_error());
    }
    let end = filesystem
        .iter()
        .position(|c| *c == 0)
        .ok_or(io::ErrorKind::InvalidData)?;
    if !matches!(
        &filesystem[..end],
        [70, 65, 84] | [70, 65, 84, 51, 50] | [101, 120, 70, 65, 84]
    ) {
        return Ok(None);
    }
    let starting = 0_i64;
    let mut allocation = Retrieval::default();
    let mut returned = 0_u32;
    // SAFETY: Win32 ABI, live synchronous handle, aligned initialized 8-byte input and 32-byte output, writable DWORD; no OVERLAPPED.
    let result = unsafe {
        DeviceIoControl(
            file.as_raw_handle(),
            0x0009_0073,
            (&raw const starting).cast(),
            8,
            (&raw mut allocation).cast(),
            32,
            &raw mut returned,
            std::ptr::null_mut(),
        )
    };
    if result == 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(234) {
            return Err(error);
        }
    }
    if returned < 32 || allocation.count == 0 || allocation.starting != 0 || allocation.next <= 0 {
        return Err(io::ErrorKind::InvalidData.into());
    }
    u64::try_from(allocation.lcn)
        .map(Some)
        .map_err(|_| io::ErrorKind::InvalidData.into())
}

/// `std::fs::rename` asks Windows to replace an existing target; this never does. The refusal is
/// ERROR_ALREADY_EXISTS or ERROR_FILE_EXISTS, both `AlreadyExists`.
pub(super) fn move_new(from: &Path, to: &Path) -> io::Result<()> {
    let from = wide(from);
    let to = wide(to);
    // SAFETY: both arguments are NUL-terminated UTF-16 buffers that live until the call returns.
    if unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), NO_REPLACE) } == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// The path as a NUL-terminated wide string. A plain drive path takes the verbatim form, which lifts the
/// 260-character limit; any other form is passed exactly as given.
fn wide(path: &Path) -> Vec<u16> {
    let mut components = path.components();
    let verbatim = match (components.next(), components.next()) {
        (Some(Component::Prefix(prefix)), Some(Component::RootDir))
            if matches!(prefix.kind(), Prefix::Disk(_)) =>
        {
            let mut text = OsString::from(r"\\?\");
            text.push(prefix.as_os_str());
            let mut plain = true;
            for component in components {
                if let Component::Normal(part) = component {
                    text.push(r"\");
                    text.push(part);
                } else {
                    plain = false;
                    break;
                }
            }
            plain.then_some(text)
        }
        _ => None,
    };
    verbatim
        .as_deref()
        .unwrap_or(path.as_os_str())
        .encode_wide()
        .chain(Some(0))
        .collect()
}
