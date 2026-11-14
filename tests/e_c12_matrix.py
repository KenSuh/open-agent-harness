from __future__ import annotations

FAULTS = ('missing', 'not-regular', 'denied', 'invalid-content', 'eio')

INPUT_MATRIX = {
    'parent-inventory': (('OAH_CAPABILITY_INVENTORY_UNKNOWN', 69), ('OAH_CAPABILITY_INVENTORY_UNKNOWN', 69),
        ('OAH_CAPABILITY_INVENTORY_UNKNOWN', 69), ('OAH_CAPABILITY_INVENTORY_UNKNOWN', 69), ('OAH_STATE_IO', 69)),
    'skill-needs': (('OAH_SKILL_NEEDS_INVALID(malformed)', 1), ('OAH_SKILL_NEEDS_INVALID(malformed)', 1),
        ('OAH_SKILL_NEEDS_INVALID(malformed)', 1), ('OAH_SKILL_NEEDS_INVALID(malformed)', 1), ('OAH_STATE_IO', 69)),
    'relay-request': (('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1),
        ('OAH_CAPABILITIES_USAGE', 1), ('OAH_STATE_IO', 69)),
    'relay-invocation': (('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1),
        ('OAH_CAPABILITIES_USAGE', 1), ('OAH_STATE_IO', 69)),
    'relay-result': (('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1),
        ('OAH_CAPABILITY_RELAY_REQUIRED(invalid)', 1), ('OAH_STATE_IO', 69)),
    'native-result': (('OAH_STATE_IO', 69), ('OAH_STATE_IO', 69), ('OAH_STATE_IO', 69),
        ('OAH_NATIVE_RESULT_INVALID(not-utf8)', 1), ('OAH_STATE_IO', 69)),
}

OWNER_MATRIX = {
    'admission': (('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_SKILL_STATE_CORRUPT', 69),
                  ('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_STATE_IO', 69)),
    'launch': (('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_RUN_LAUNCH_INVALID', 1),
               ('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_STATE_IO', 69)),
    'freeze': (('OAH_FIX_STATE_CHANGED', 1), ('OAH_FIX_STATE_CHANGED', 1),
               ('OAH_FIX_STATE_CHANGED', 1), ('OAH_FIX_STATE_CHANGED', 1), ('OAH_STATE_IO', 69)),
    'settle': (('OAH_NATIVE_OFFER_STALE', 1), ('OAH_NATIVE_OFFER_STALE', 1),
               ('OAH_NATIVE_OFFER_STALE', 1), ('OAH_NATIVE_OFFER_STALE', 1), ('OAH_STATE_IO', 69)),
    'relay': (('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1),
              ('OAH_CAPABILITIES_USAGE', 1), ('OAH_CAPABILITIES_USAGE', 1), ('OAH_STATE_IO', 69)),
    'skills': (('OAH_SKILLS_IO', 69), ('OAH_SKILLS_IO', 69),
               ('OAH_SKILLS_IO', 69), ('OAH_SKILLS_IO', 69), ('OAH_STATE_IO', 69)),
    'worker': (('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_RUN_LAUNCH_INVALID', 1),
               ('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_STATE_IO', 69)),
    'design': (('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_SKILL_STATE_CORRUPT', 69),
               ('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_STATE_IO', 69)),
    'wait': (('OAH_RUN_CORRUPT', 1), ('OAH_RUN_CORRUPT', 1),
             ('OAH_RUN_CORRUPT', 1), ('OAH_RUN_CORRUPT', 1), ('OAH_STATE_IO', 69)),
}

OWNER_RUN_MATRIX = {
    'admission': ('preparation', 'preparation', 'preparation', 'preparation', 'preparation'),
    'launch': ('refused-or-unreadable-result-kept', 'refused-or-unreadable-result-kept',
               'refused-or-unreadable-result-kept', 'refused-or-unreadable-result-kept',
               'refused-or-unreadable-result-kept'),
    'freeze': ('original-kept', 'original-kept', 'original-kept', 'original-kept', 'original-kept'),
    'settle': ('kept', 'kept', 'kept', 'kept', 'kept'),
    'relay': ('kept', 'kept', 'kept', 'kept', 'kept'),
    'skills': ('kept', 'kept', 'kept', 'kept', 'kept'),
    'worker': ('refused-or-stale-result-claimed', 'refused-or-stale-result-claimed',
               'refused-or-stale-result-claimed', 'refused-or-stale-result-claimed',
               'refused-or-unreadable-result-unclaimed'),
    'design': ('kept', 'kept', 'kept', 'kept', 'kept'),
    'wait': ('kept', 'kept', 'kept', 'kept', 'kept'),
}

OWNER_KEPT_MATRIX = {
    'admission': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'launch': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'freeze': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'settle': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'relay': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'skills': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'worker': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'design': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
    'wait': ('inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access', 'inputs-at-access'),
}

OWNER_RULES = {
    'admission': (
        ('BudgetRefused', 'preserve'), ('PackRefused', 'preserve'), ('CapabilityRefused', 'preserve'),
        ('NeedsInvalid', 'preserve'), ('StoreProblem', 'preserve'), ('ScratchError', 'preserve'),
        ('EngineError', 'preserve'), ('RecordRefusal', 'OAH_SKILL_STATE_CORRUPT'), ('OSError', 'OAH_STATE_IO')),
    'launch': (
        ('RecordRefusal', 'OAH_RUN_LAUNCH_INVALID'), ('StateCorrupt', 'OAH_RUN_LAUNCH_INVALID'),
        ('StoreProblem', 'OAH_RUN_LAUNCH_INVALID'), ('ConfigError', 'OAH_SKILL_NOT_DELIVERABLE(changed)'),
        ('RoutingError', 'OAH_SKILL_NOT_DELIVERABLE(changed)'), ('RunError', 'OAH_RUN_LAUNCH_INVALID'),
        ('BudgetRefused', 'OAH_SKILL_NOT_DELIVERABLE(changed)'), ('PackRefused', 'OAH_SKILL_NOT_DELIVERABLE(changed)'),
        ('CapabilityRefused', 'OAH_RUN_LAUNCH_INVALID'), ('ValueError', 'OAH_SKILL_NOT_DELIVERABLE(changed)'),
        ('ScratchError', 'preserve'), ('EngineError', 'preserve'), ('OSError', 'OAH_STATE_IO')),
    'freeze': (
        ('RecordRefusal', 'OAH_FIX_STATE_CHANGED'), ('StateCorrupt', 'OAH_FIX_STATE_CHANGED'),
        ('StoreProblem', 'OAH_FIX_STATE_CHANGED'), ('ConfigError', 'OAH_FIX_STATE_CHANGED'),
        ('RoutingError', 'OAH_FIX_STATE_CHANGED'), ('RunError', 'OAH_FIX_STATE_CHANGED'),
        ('BudgetRefused', 'OAH_FIX_STATE_CHANGED'), ('PackRefused', 'OAH_FIX_STATE_CHANGED'),
        ('CapabilityRefused', 'OAH_FIX_STATE_CHANGED'), ('ValueError', 'OAH_FIX_STATE_CHANGED'),
        ('ScratchError', 'preserve'), ('EngineError', 'preserve'), ('OSError', 'OAH_STATE_IO')),
    'settle': (
        ('ProtocolRefused', 'preserve'), ('ScratchError', 'preserve'), ('RecordRefusal', 'OAH_NATIVE_OFFER_STALE'),
        ('StateCorrupt', 'OAH_NATIVE_OFFER_STALE'), ('StoreProblem', 'OAH_NATIVE_OFFER_STALE'),
        ('ConfigError', 'OAH_NATIVE_OFFER_STALE'), ('RoutingError', 'OAH_NATIVE_OFFER_STALE'),
        ('RunError', 'OAH_NATIVE_OFFER_STALE'), ('BudgetRefused', 'OAH_NATIVE_OFFER_STALE'),
        ('PackRefused', 'OAH_NATIVE_OFFER_STALE'), ('CapabilityRefused', 'OAH_NATIVE_OFFER_STALE'),
        ('EngineError', 'OAH_NATIVE_OFFER_STALE'), ('ValueError', 'OAH_NATIVE_OFFER_STALE'), ('OSError', 'OAH_STATE_IO')),
    'relay': (('RecordRefusal', 'OAH_CAPABILITIES_USAGE'), ('CapabilityRefused', 'preserve'),
              ('StoreProblem', 'preserve'), ('OSError', 'OAH_STATE_IO')),
    'skills': (('RecordRefusal', 'OAH_SKILLS_IO'), ('RoutingError', 'preserve')),
    'worker': (('RecordRefusal', 'OAH_RUN_LAUNCH_INVALID'),),
    'design': (('RecordRefusal', 'OAH_SKILL_STATE_CORRUPT'),),
    'wait': (('RecordRefusal', 'OAH_RUN_CORRUPT'),),
}

ROW_REACH = set()

OWNER_SURFACES = {'admission': ('process', 'native', 'orca'), 'launch': ('process',), 'freeze': ('fix',),
                  'settle': ('native',), 'relay': ('capabilities',), 'skills': ('skills',), 'worker': ('orca',),
                  'design': ('coverage',), 'wait': ('wait',)}

AFTER_MATRIX = {
    ('launch', 'after-engine'): (
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_STATE_IO', 69, 'at-access-kept', 'inputs-at-access', 1, 0)),
    ('launch', 'after-runner'): (
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_STATE_IO', 69, 'at-access-kept', 'inputs-at-access', 0, 1)),
    ('admission', 'after-runner'): (
        ('OAH_SKILL_STATE_CORRUPT', 69, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_SKILL_STATE_CORRUPT', 69, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_SKILL_STATE_CORRUPT', 69, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_SKILL_STATE_CORRUPT', 69, 'at-access-kept', 'inputs-at-access', 0, 1),
        ('OAH_STATE_IO', 69, 'at-access-kept', 'inputs-at-access', 0, 1)),
    ('worker', 'after-engine'): (
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept', 'inputs-at-access', 1, 0),
        ('OAH_STATE_IO', 69, 'at-access-kept', 'inputs-at-access', 1, 0)),
}

SUCCESS_DISPATCH = {
    'admission': (1, 0), 'admission-native': (0, 0), 'admission-orca': (0, 1),
    'admission-needs': (1, 0), 'admission-saved': (1, 0), 'launch': (1, 0), 'background': (0, 1),
    'worker': (1, 0), 'freeze': (1, 0), 'settle': (0, 0), 'settle-retry': (0, 0),
    'skills': (0, 0), 'skills-receiving': (0, 0), 'skills-repeat': (0, 0), 'skills-plan': (0, 0),
    'skills-read-review': (0, 0), 'skills-candidates': (0, 0), 'skills-review': (0, 0),
    'skills-select': (0, 0), 'skills-find': (0, 0), 'capabilities-plan': (0, 0),
    'relay': (0, 0), 'design': (0, 0), 'wait': (0, 0),
}

OUTPUT_MATRIX = {
    'ignored-address': (('', 0), ('', 0), ('', 0)),
    'address-only': (('', 0), ('OAH_STATE_IO', 69), ('OAH_STATE_IO', 69)),
    'launch-read-after-address': (('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_STATE_IO', 69)),
    'launch-address-before-read': (('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_STATE_IO', 69), ('OAH_STATE_IO', 69)),
    'admission-read-after-address': (('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_STATE_IO', 69)),
    'admission-address-before-read': (('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_STATE_IO', 69), ('OAH_STATE_IO', 69)),
    'optional-summary-before-launch': (('OAH_RUN_LAUNCH_INVALID', 1), ('OAH_SKILL_STATE_CORRUPT', 69), ('OAH_STATE_IO', 69)),
    'captured-result': (('OAH_FIX_STATE_CHANGED', 1), ('OAH_STATE_IO', 69), ('OAH_STATE_IO', 69)),
}
