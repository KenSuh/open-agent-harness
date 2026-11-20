// Windows 비공개 확인의 순수 경계·경로 판정: SID·ACL 버퍼 경계와 고정 드라이브 루트 정규화를 플랫폼 독립적으로 검증한다.
//! Pure Windows FFI bounds and drive-root normalization rules, compiled on Windows and in all test builds.

pub(super) const SID_REVISION: u8 = 1;
pub(super) const SID_HEADER_BYTES: usize = 8;
pub(super) const SID_MAX_SUB_AUTHORITIES: usize = 15;
pub(super) const MAX_ACL_BYTES: usize = u16::MAX as usize;
#[cfg(windows)]
pub(super) const MAX_WIDE_UNITS: usize = 32_768;

/// Checks that a `GetTokenInformation(TokenUser)` output buffer has a valid size and an aligned SID pointer
/// inside the returned portion after the `TOKEN_USER` header. Returns the SID's byte offset from `base_addr`.
pub(super) fn validate_token_sid_bounds(
    base_addr: usize,
    buf_len: usize,
    returned: usize,
    min_offset: usize,
    sid_addr: usize,
    sid_aligned: bool,
) -> Result<usize, &'static str> {
    if returned < min_offset || returned > buf_len {
        return Err("GetTokenInformation returned an invalid buffer size");
    }
    if !sid_aligned {
        return Err("GetTokenInformation returned an unaligned SID");
    }
    let offset = sid_addr
        .checked_sub(base_addr)
        .ok_or("GetTokenInformation returned a SID outside its buffer")?;
    if offset < min_offset
        || offset
            .checked_add(SID_HEADER_BYTES)
            .is_none_or(|e| e > returned)
    {
        return Err("GetTokenInformation returned a SID outside the returned bytes");
    }
    Ok(offset)
}

/// Checks a Win32 SID header (`Revision == 1`, `SubAuthorityCount <= 15`) and verifies the full SID byte length
/// fits inside `[sid_offset, container_len]`. Returns the expected SID byte length.
pub(super) fn validate_sid_layout(
    sid_offset: usize,
    container_len: usize,
    revision: u8,
    sub_auth_count: u8,
) -> Result<usize, &'static str> {
    if revision != SID_REVISION {
        return Err("unsupported SID revision");
    }
    let count = usize::from(sub_auth_count);
    if count > SID_MAX_SUB_AUTHORITIES {
        return Err("SID sub-authority count exceeds Win32 maximum");
    }
    let len = SID_HEADER_BYTES + count * 4;
    if sid_offset
        .checked_add(len)
        .is_none_or(|end| end > container_len)
    {
        return Err("SID extends past its containing buffer");
    }
    Ok(len)
}

/// Checks `ACL_SIZE_INFORMATION` returned by `GetAclInformation(AclSizeInformation)`.
/// Returns `bytes_in_use` as `usize`.
pub(super) fn validate_acl_size(
    ace_count: u32,
    bytes_in_use: u32,
    acl_header_len: usize,
    ace_header_len: usize,
) -> Result<usize, &'static str> {
    let bytes = usize::try_from(bytes_in_use).map_err(|_| "ACL byte count overflow")?;
    if bytes < acl_header_len || bytes > MAX_ACL_BYTES {
        return Err("ACL byte count is out of bounds");
    }
    let count = usize::try_from(ace_count).map_err(|_| "ACL entry count overflow")?;
    let max_entries = (bytes - acl_header_len) / ace_header_len;
    if count > max_entries {
        return Err("ACL entry count exceeds ACL byte capacity");
    }
    Ok(bytes)
}

/// Checks that an ACE pointer returned by `GetAce` starts after the ACL header and leaves room for `ACE_HEADER`
/// inside `acl_bytes`. Returns the ACE byte offset from `acl_start`.
pub(super) fn validate_ace_header_bounds(
    acl_start: usize,
    acl_bytes: usize,
    acl_header_len: usize,
    ace_header_len: usize,
    ace_addr: usize,
) -> Result<usize, &'static str> {
    let offset = ace_addr
        .checked_sub(acl_start)
        .ok_or("ACE pointer lies before its ACL")?;
    if offset < acl_header_len
        || offset
            .checked_add(ace_header_len)
            .is_none_or(|end| end > acl_bytes)
    {
        return Err("ACE header extends past its ACL");
    }
    Ok(offset)
}

/// Checks that `header.AceSize` covers at least `ACE_HEADER` and stays within `acl_bytes`.
/// Returns `ace_size` as `usize`.
pub(super) fn validate_ace_extent(
    acl_start: usize,
    acl_bytes: usize,
    ace_addr: usize,
    ace_header_len: usize,
    ace_size: u16,
) -> Result<usize, &'static str> {
    let offset = ace_addr
        .checked_sub(acl_start)
        .ok_or("ACE pointer lies before its ACL")?;
    let size = usize::from(ace_size);
    if size < ace_header_len || offset.checked_add(size).is_none_or(|end| end > acl_bytes) {
        return Err("ACE size extends past its ACL");
    }
    Ok(size)
}

/// Checks that an `ACCESS_ALLOWED_ACE` is large enough for its struct and an aligned SID header at `sid_offset`.
pub(super) fn validate_allow_ace_sid_offset(
    ace_size: usize,
    sid_offset: usize,
    struct_len: usize,
    sid_aligned: bool,
) -> Result<(), &'static str> {
    if !sid_aligned {
        return Err("allowing ACE contains an unaligned SID");
    }
    if ace_size < struct_len
        || sid_offset
            .checked_add(SID_HEADER_BYTES)
            .is_none_or(|e| e > ace_size)
    {
        return Err("allowing ACE is too small for a SID header");
    }
    Ok(())
}

/// Extracts an explicit normal NUL-terminated Windows drive root (`<DRIVE>:\`, e.g. `C:\`) suitable for
/// `GetDriveTypeW` from a UTF-16 path.
///
/// Accepts only:
/// * a normal disk prefix followed by a root separator (`<A-Za-z>:\...` or `<A-Za-z>:/...`), or
/// * a verbatim disk prefix followed by a backslash root (`\\?\<A-Za-z>:\...`).
///
/// Refuses embedded NULs, relative paths, drive-relative paths (`C:` or `C:foo`), UNC paths (`\\server\share`,
/// `\\?\UNC\...`), device paths (`\\.\...`), volume GUID paths (`\\?\Volume{...}\...`), and `GLOBALROOT`.
pub(super) fn windows_drive_root_units(units: &[u16]) -> Option<[u16; 4]> {
    if units.contains(&0) {
        return None;
    }
    let letter = match units {
        // Verbatim disk prefix `\\?\X:\...`: verbatim paths require an exact backslash root separator.
        [0x5c, 0x5c, 0x3f, 0x5c, drive, 0x3a, 0x5c, ..] => ascii_drive_letter(*drive)?,
        // Normal disk prefix `X:\...` or `X:/...`.
        [drive, 0x3a, 0x5c | 0x2f, ..] => ascii_drive_letter(*drive)?,
        _ => return None,
    };
    Some([u16::from(letter), u16::from(b':'), u16::from(b'\\'), 0])
}

fn ascii_drive_letter(unit: u16) -> Option<u8> {
    let byte = u8::try_from(unit).ok()?;
    byte.is_ascii_alphabetic()
        .then_some(byte.to_ascii_uppercase())
}

#[cfg(test)]
pub(super) fn windows_drive_root_str(path: &str) -> Option<String> {
    let units: Vec<u16> = path.encode_utf16().collect();
    let root = windows_drive_root_units(&units)?;
    String::from_utf16(&root[..3]).ok()
}
