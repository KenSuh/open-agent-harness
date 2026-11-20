// 비공개 확인의 순수 규칙 단위 시험: 위치·정리·Windows FFI 버퍼 경계·고정 드라이브 정규화 규칙을 모든 플랫폼에서 검증한다.
use super::rules::{
    ALLOW_ACE_TYPE, CleanupCheck, Entry, Security, Who, location_rule, may_cleanup,
    private_folder_rule, trusted_location_rule,
};
use super::windows_rules::{
    DescriptorTarget, SID_HEADER_BYTES, SID_MAX_SUB_AUTHORITIES, SID_REVISION, private_sddl_str,
    validate_ace_extent, validate_ace_header_bounds, validate_acl_size,
    validate_allow_ace_sid_offset, validate_sid_layout, validate_token_sid_bounds,
    windows_drive_root_str,
};

#[test]
fn the_location_rule_admits_only_root_or_this_user_and_sticky_shared_folders() {
    assert_eq!(location_rule(true, 0, 501, false, false), Ok(()));
    assert_eq!(location_rule(true, 501, 501, false, false), Ok(()));
    assert_eq!(location_rule(true, 0, 501, true, true), Ok(()));
    assert_eq!(location_rule(true, 501, 501, true, true), Ok(()));
    assert!(location_rule(false, 501, 501, false, false).is_err());
    assert!(location_rule(true, 502, 501, false, false).is_err());
    assert!(location_rule(true, 502, 501, true, true).is_err());
    assert!(location_rule(true, 0, 501, true, false).is_err());
    assert!(location_rule(true, 501, 501, true, false).is_err());
}

#[test]
fn cleanup_requires_opened_handle_expected_type_owner_and_intact_identity() {
    let verified = CleanupCheck {
        opened: true,
        expected_type: true,
        owner_is_user: true,
        identity_intact: true,
    };
    assert!(may_cleanup(verified));
    // Insufficient ACL policy after owner and directory identity are proven still allows verified cleanup.
    let weak_dacl = security(Who::User, false, &[(ALLOW_ACE_TYPE, false, Who::User)]);
    assert!(private_folder_rule(&weak_dacl).is_err());
    assert!(may_cleanup(CleanupCheck {
        owner_is_user: weak_dacl.owner == Who::User,
        ..verified
    }));
    // Insufficient ACL policy when owner is not proven must leave the entry untouched.
    let other_owner = security(Who::Other, true, &[(ALLOW_ACE_TYPE, false, Who::User)]);
    assert!(private_folder_rule(&other_owner).is_err());
    assert!(!may_cleanup(CleanupCheck {
        owner_is_user: other_owner.owner == Who::User,
        ..verified
    }));
    for unverified in [
        CleanupCheck {
            opened: false,
            ..verified
        },
        CleanupCheck {
            expected_type: false,
            ..verified
        },
        CleanupCheck {
            owner_is_user: false,
            ..verified
        },
        CleanupCheck {
            identity_intact: false,
            ..verified
        },
        CleanupCheck::unverified(),
    ] {
        assert!(!may_cleanup(unverified), "{unverified:?}");
    }
}

fn security(owner: Who, protected: bool, entries: &[(u8, bool, Who)]) -> Security {
    let entries = entries.iter().map(|&(kind, inherit_only, who)| Entry {
        kind,
        inherit_only,
        who,
    });
    Security {
        owner,
        dacl_present: true,
        protected,
        entries: entries.collect(),
    }
}

#[test]
fn the_private_folder_rule_admits_only_an_owned_protected_list_for_this_user() {
    let mine = [(ALLOW_ACE_TYPE, false, Who::User)];
    assert_eq!(
        private_folder_rule(&security(Who::User, true, &mine)),
        Ok(())
    );
    let with_deny = [
        (0x01, false, Who::Other),
        (ALLOW_ACE_TYPE, false, Who::User),
    ];
    assert_eq!(
        private_folder_rule(&security(Who::User, true, &with_deny)),
        Ok(())
    );
    assert!(private_folder_rule(&security(Who::Trusted, true, &mine)).is_err());
    assert!(private_folder_rule(&security(Who::User, false, &mine)).is_err());
    let mut missing = security(Who::User, true, &mine);
    missing.dacl_present = false;
    assert!(private_folder_rule(&missing).is_err());
    for who in [Who::Trusted, Who::Other] {
        for inherit_only in [false, true] {
            let entry = [(ALLOW_ACE_TYPE, inherit_only, who)];
            assert!(private_folder_rule(&security(Who::User, true, &entry)).is_err());
        }
    }
    for other_allowing_type in [0x04, 0x05, 0x09, 0x0B] {
        let entry = [(other_allowing_type, false, Who::Other)];
        assert!(private_folder_rule(&security(Who::User, true, &entry)).is_err());
    }
}

#[test]
fn the_location_rule_on_windows_admits_this_user_system_and_administrators_only() {
    let standard = [
        (ALLOW_ACE_TYPE, false, Who::Trusted),
        (ALLOW_ACE_TYPE, false, Who::User),
    ];
    for owner in [Who::User, Who::Trusted] {
        assert_eq!(
            trusted_location_rule(&security(owner, false, &standard)),
            Ok(())
        );
    }
    assert!(trusted_location_rule(&security(Who::Other, false, &standard)).is_err());
    let for_children = [
        (ALLOW_ACE_TYPE, true, Who::Other),
        (ALLOW_ACE_TYPE, false, Who::User),
    ];
    assert_eq!(
        trusted_location_rule(&security(Who::User, false, &for_children)),
        Ok(())
    );
    let other = [(ALLOW_ACE_TYPE, false, Who::Other)];
    assert!(trusted_location_rule(&security(Who::User, false, &other)).is_err());
    let object = [(0x05, false, Who::Other)];
    assert!(trusted_location_rule(&security(Who::User, false, &object)).is_err());
    let mut missing = security(Who::User, false, &standard);
    missing.dacl_present = false;
    assert!(trusted_location_rule(&missing).is_err());
}

#[test]
fn windows_token_and_sid_bounds_reject_short_oversized_unaligned_and_truncated_data() {
    let base = 0x1000_usize;
    assert_eq!(
        validate_token_sid_bounds(base, 512, 44, 16, base + 16, true),
        Ok(16)
    );
    assert!(validate_token_sid_bounds(base, 512, 12, 16, base + 16, true).is_err());
    assert!(validate_token_sid_bounds(base, 512, 520, 16, base + 16, true).is_err());
    assert!(validate_token_sid_bounds(base, 512, 44, 16, base + 16, false).is_err());
    assert!(validate_token_sid_bounds(base, 512, 44, 16, 0, true).is_err());
    assert!(validate_token_sid_bounds(base, 512, 44, 16, base + 8, true).is_err());
    assert!(validate_token_sid_bounds(base, 512, 20, 16, base + 16, true).is_err());

    assert_eq!(validate_sid_layout(16, 44, SID_REVISION, 5), Ok(28));
    assert_eq!(
        validate_sid_layout(
            0,
            SID_HEADER_BYTES + SID_MAX_SUB_AUTHORITIES * 4,
            SID_REVISION,
            15
        ),
        Ok(68)
    );
    assert!(validate_sid_layout(16, 44, 2, 5).is_err());
    assert!(validate_sid_layout(16, 128, SID_REVISION, 16).is_err());
    assert!(validate_sid_layout(16, 40, SID_REVISION, 5).is_err());
}

#[test]
fn windows_acl_and_ace_bounds_reject_malformed_sizes_offsets_and_alignment() {
    assert_eq!(validate_acl_size(0, 8, 8, 4), Ok(8));
    assert_eq!(validate_acl_size(2, 64, 8, 4), Ok(64));
    assert!(validate_acl_size(1, 4, 8, 4).is_err());
    assert!(validate_acl_size(1, 8, 8, 4).is_err());
    assert!(validate_acl_size(0, 70_000, 8, 4).is_err());

    let acl = 0x2000_usize;
    assert_eq!(validate_ace_header_bounds(acl, 64, 8, 4, acl + 8), Ok(8));
    assert!(validate_ace_header_bounds(acl, 64, 8, 4, acl - 4).is_err());
    assert!(validate_ace_header_bounds(acl, 64, 8, 4, acl + 4).is_err());
    assert!(validate_ace_header_bounds(acl, 64, 8, 4, acl + 62).is_err());

    assert_eq!(validate_ace_extent(acl, 64, acl + 8, 4, 36), Ok(36));
    assert!(validate_ace_extent(acl, 64, acl + 8, 4, 2).is_err());
    assert!(validate_ace_extent(acl, 64, acl + 8, 4, 60).is_err());

    assert_eq!(validate_allow_ace_sid_offset(36, 8, 12, true), Ok(()));
    assert!(validate_allow_ace_sid_offset(36, 8, 12, false).is_err());
    assert!(validate_allow_ace_sid_offset(10, 8, 12, true).is_err());
    assert!(validate_allow_ace_sid_offset(12, 8, 12, true).is_err());
}

#[test]
fn windows_drive_root_normalization_accepts_fixed_disk_forms_and_fails_closed_otherwise() {
    for (input, expected) in [
        (r"C:\LocalData\App\Temp", Some("C:\\")),
        (r"c:\Temp", Some("C:\\")),
        ("d:/Temp", Some("D:\\")),
        (r"\\?\C:\LocalData\App\Temp", Some("C:\\")),
        (r"\\?\z:\Temp", Some("Z:\\")),
    ] {
        assert_eq!(
            windows_drive_root_str(input).as_deref(),
            expected,
            "{input}"
        );
    }
    for refused in [
        "",
        r"relative\Temp",
        r"\Temp",
        "/tmp",
        "C:",
        "C:Temp",
        r"C:LocalData\App",
        r"\\?\C:",
        r"\\?\C:Temp",
        r"\\?\C:/Temp",
        r"\\server\share\Temp",
        r"\\?\UNC\server\share\Temp",
        r"\\.\C:\Temp",
        r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\Temp",
        r"\\?\GLOBALROOT\Device\HarddiskVolume1\Temp",
        r"1:\Temp",
        "C:\\Temp\0extra",
    ] {
        assert_eq!(windows_drive_root_str(refused), None, "{refused}");
    }
}

#[test]
fn windows_private_sddl_sets_explicit_user_owner_for_directory_and_copy() {
    let sid = "S-1-5-21-100-200-300-1001";
    assert_eq!(
        private_sddl_str(sid, DescriptorTarget::Directory).as_deref(),
        Some("O:S-1-5-21-100-200-300-1001D:P(A;OICI;FA;;;S-1-5-21-100-200-300-1001)")
    );
    assert_eq!(
        private_sddl_str(sid, DescriptorTarget::File).as_deref(),
        Some("O:S-1-5-21-100-200-300-1001D:P(A;;FA;;;S-1-5-21-100-200-300-1001)")
    );
    assert!(private_sddl_str("S-1-0x123456789abc-1000", DescriptorTarget::File).is_some());
    for invalid in [
        "",
        "S-1-",
        "S-1-5-",
        "S-1-5--1",
        "S-2-5-1",
        "BA",
        "S-1-5-21)\0",
        "S-1-5-21(A;;FA;;;WD)",
    ] {
        assert_eq!(private_sddl_str(invalid, DescriptorTarget::Directory), None);
        assert_eq!(private_sddl_str(invalid, DescriptorTarget::File), None);
    }
}
