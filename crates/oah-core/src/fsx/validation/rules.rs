// 비공개 확인의 순수 판정: 운영체제 호출 없이 소유자·권한 비트·접근 목록 항목만으로 위치와 폴더를 판정한다.
//! Pure rules of the private check, kept apart so every platform's tests exercise all of them.

/// Why a Unix folder on the way to the private copy lets someone other than root or this user change its
/// entries. It must be a folder that belongs to root or this user, and others may write it only when the sticky
/// bit keeps them from renaming or removing entries they do not own.
#[cfg(any(unix, test))]
pub(super) fn location_rule(
    folder: bool,
    owner: u32,
    user: u32,
    others_write: bool,
    sticky: bool,
) -> Result<(), &'static str> {
    if !folder {
        return Err("it is not a folder");
    }
    if owner != 0 && owner != user {
        return Err("it belongs to another user");
    }
    if others_write && !sticky {
        return Err("other users can rename or replace its entries");
    }
    Ok(())
}

/// Whom a Windows SID names, as far as the private check cares.
#[cfg(any(windows, test))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Who {
    /// This process's user.
    User,
    /// SYSTEM or the Administrators group, both outside the guarantee.
    Trusted,
    Other,
}

/// One access-list entry: its type, whether it only passes to new children (`INHERIT_ONLY_ACE`), and whom a plain
/// allowing entry names (`Other` for every other type).
#[cfg(any(windows, test))]
#[derive(Debug, Clone, Copy)]
pub(super) struct Entry {
    pub kind: u8,
    pub inherit_only: bool,
    pub who: Who,
}

/// A Windows folder's owner and access list, read through its handle.
#[cfg(any(windows, test))]
#[derive(Debug)]
pub(super) struct Security {
    pub owner: Who,
    /// False for a missing (null) access list, which admits everyone.
    pub dacl_present: bool,
    pub protected: bool,
    pub entries: Vec<Entry>,
}

/// `ACCESS_ALLOWED_ACE_TYPE`.
#[cfg(any(windows, test))]
pub(super) const ALLOW_ACE_TYPE: u8 = 0x00;

/// The access-list entry types that only deny: `ACCESS_DENIED`, `ACCESS_DENIED_OBJECT`, `ACCESS_DENIED_CALLBACK`
/// and `ACCESS_DENIED_CALLBACK_OBJECT`.
#[cfg(any(windows, test))]
const DENY_ACE_TYPES: [u8; 4] = [0x01, 0x06, 0x0A, 0x0C];

/// The private folder this start created: owned by this user, with a protected access list whose allowing
/// entries, including those its children inherit, all name this user.
#[cfg(any(windows, test))]
pub(super) fn private_folder_rule(security: &Security) -> Result<(), &'static str> {
    if security.owner != Who::User {
        return Err("it is not owned by this user");
    }
    if !security.protected {
        return Err("its access list can inherit entries from the folder above");
    }
    allows_only(security, &[Who::User], false)
}

/// The per-user location: owned by this user, SYSTEM or Administrators, and allowing only them to use the folder
/// itself. Entries that only pass to new children do not matter, since the private folder inherits nothing.
#[cfg(any(windows, test))]
pub(super) fn trusted_location_rule(security: &Security) -> Result<(), &'static str> {
    if security.owner == Who::Other {
        return Err("it is not owned by this user, SYSTEM or Administrators");
    }
    allows_only(security, &[Who::User, Who::Trusted], true)
}

#[cfg(any(windows, test))]
fn allows_only(
    security: &Security,
    allowed: &[Who],
    skip_inherit_only: bool,
) -> Result<(), &'static str> {
    if !security.dacl_present {
        return Err("it has no access list, which admits everyone");
    }
    for entry in &security.entries {
        let harmless =
            DENY_ACE_TYPES.contains(&entry.kind) || (skip_inherit_only && entry.inherit_only);
        if !harmless && (entry.kind != ALLOW_ACE_TYPE || !allowed.contains(&entry.who)) {
            return Err("its access list admits someone else");
        }
    }
    Ok(())
}
