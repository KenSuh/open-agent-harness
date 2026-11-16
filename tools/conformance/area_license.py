# license 영역: 라이선스 전문 분류(지원 5종과 거부), 선언식 해석, 증거에서 낸 판정과 허용 목록별 적격성을 사례로 만든다.
"""Area `license`: whole-text classification, declared expressions, verdicts and eligibility under allow lists."""
from __future__ import annotations

import hashlib
import pathlib

import texts
from fixture import Area, Case, fn, ok


def build(base: pathlib.Path) -> Area:
    from oah.design.sources import license as lic

    apache = texts.apache(pathlib.Path(__file__).resolve().parents[2])
    cases: list[Case] = []

    classify = {
        "mit": texts.MIT.encode(), "isc": texts.ISC.encode(), "bsd-2-clause": texts.BSD2.encode(),
        "bsd-3-clause": texts.BSD3.encode(), "apache-2-0": apache.encode(), "gpl-title": texts.GPL.encode(),
        "mit-crlf": texts.MIT.replace("\n", "\r\n").encode(),
        "mit-notice-range": texts.MIT.replace("Copyright (c) 2026 Acme",
                                              "Copyright (c) 2015-2026 Acme, Inc.\nAll rights reserved.").encode(),
        "mit-notice-short": texts.MIT.replace("Copyright (c) 2026 Acme", "(c) 2026 Acme").encode(),
        "mit-notice-symbol": texts.MIT.replace("Copyright (c) 2026 Acme", "Copyright © 2026 Acme").encode(),
        "mit-multiword-holder": texts.MIT.replace("2026 Acme", "2026 Jane Doe").encode(),
        "mit-extra-terms": (texts.MIT + "\nYou may not use this software to build weapons.\n").encode(),
        "mit-parenthesized-terms": texts.MIT.replace("2026 Acme", "2026 Acme (commercial use is forbidden)").encode(),
        "mit-copyright-clause": texts.MIT.replace("2026 Acme", "2026 Acme; permission required").encode(),
        "mit-snippet": texts.MIT.split("\n\n")[2].encode(),
        "mit-hidden-character": texts.MIT.replace("Permission", "Permission‮", 1).encode(),
        "mit-utf16": b"\xff\xfe" + texts.MIT.encode("utf-16-le"),
        "mit-and-isc-in-one-file": (texts.MIT + "\n" + texts.ISC).encode(),
        "empty": b"",
    }
    rows, results = {}, {}
    for key, data in classify.items():
        result = lic.classify_text(data)
        value = {"spdx": result.spdx, "strict": result.strict, "problem": result.problem}
        try:
            rows[key] = {"text": data.decode("utf-8")}
            results[key] = value
        except UnicodeDecodeError:  # bytes that are not UTF-8 get a case of their own
            cases.append(Case(f"classify-{key}", ok(value), files={"LICENSE": data},
                              function="oah.design.sources.license.classify_text",
                              note="the input file LICENSE holds the exact bytes"))
    cases.append(Case("classify", ok(results), {"calls": rows}, function="oah.design.sources.license.classify_text",
                      note="a table: each row passes the UTF-8 bytes of `text`"))

    expressions, parsed_all = {}, {}
    for key, text in (("single", "MIT"), ("choice", "MIT OR Apache-2.0"), ("conjunction-lowercase", "mit and isc"),
                      ("slash", "MIT/Apache-2.0"), ("parenthesized", "(MIT OR ISC)"),
                      ("mixed-operators", "MIT OR ISC AND Apache-2.0"),
                      ("with-exception", "GPL-2.0 WITH Classpath-exception-2.0"), ("unknown-id", "Foo-1.0"),
                      ("repeated", "MIT OR MIT"), ("nested", "(MIT OR (ISC))"), ("empty", ""),
                      ("dangling", "MIT OR"), ("other-license", "GPL-3.0-only")):
        parsed = lic.parse_expression(text)
        expressions[key] = {"text": text}
        parsed_all[key] = None if parsed is None else [parsed[0], list(parsed[1])]
    cases.append(Case("expression", ok(parsed_all), {"calls": expressions},
                      function="oah.design.sources.license.parse_expression", note="a table of parse_expression calls"))

    def verdict(files: dict, scopes: list, declared: str | None = None, problems: tuple = (), missing: tuple = ()):
        blobs, listed = {}, []
        for path, text in files.items():
            data = text.encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            if path not in missing:
                blobs[digest] = data
            listed.append({"path": path, "blob": "0" * 40, "size": len(data), "sha256": digest})
        evidence = {"scopes": scopes, "files": listed, "notices": [], "problems": list(problems),
                    "declared": None if declared is None else {"path": "Cargo.toml", "field": "package.license",
                                                               "value": declared}}
        return evidence, blobs, lic.verdict(evidence, blobs.get)

    def scope(*files: str, path: str = "src", nested: tuple = ()) -> dict:
        return {"path": path, "level": "", "files": list(files), "nested": list(nested)}

    both = {"LICENSE-MIT": texts.MIT, "LICENSE-ISC": texts.ISC}
    evidence_cases = {
        "mit": ({"LICENSE": texts.MIT}, [scope("LICENSE")], None, (), (), [["MIT"], ["ISC"], []]),
        "choice": (both, [scope("LICENSE-ISC", "LICENSE-MIT")], "MIT OR ISC", (), (), [["ISC"], ["MIT", "ISC"], []]),
        "conjunction": (both, [scope("LICENSE-ISC", "LICENSE-MIT")], "MIT AND ISC", (), (), [["MIT"], ["ISC", "MIT"]]),
        "gpl": ({"COPYING": texts.GPL}, [scope("COPYING")], None, (), (), [["MIT", "ISC"]]),
        "two-texts-nothing-declared": (both, [scope("LICENSE-ISC", "LICENSE-MIT")], None, (), (), [["MIT", "ISC"]]),
        "declared-differs": ({"LICENSE": texts.MIT}, [scope("LICENSE")], "MIT OR ISC", (), (), [["MIT", "ISC"]]),
        "nested-disagrees": ({"LICENSE": texts.MIT, "src/vendor/LICENSE": texts.GPL},
                             [scope("LICENSE", nested=("src/vendor/LICENSE",))], None, (), (), [["MIT", "ISC"]]),
        "scopes-differ": ({"LICENSE": texts.MIT, "lib/LICENSE": texts.ISC},
                          [scope("LICENSE"), scope("lib/LICENSE", path="lib")], None, (), (), [["MIT", "ISC"]]),
        "evidence-problem": ({"LICENSE": texts.MIT}, [scope("LICENSE")], None, ("src holds a LICENSES folder",), (),
                             [["MIT", "ISC"]]),
        "stored-text-missing": ({"LICENSE": texts.MIT}, [scope("LICENSE")], None, (), ("LICENSE",), [["MIT", "ISC"]]),
        "no-license-file": ({}, [scope()], None, (), (), [["MIT", "ISC"]]),
    }
    rows, results = {}, {}
    for key, (files, scopes, declared, problems, missing, allows) in evidence_cases.items():
        evidence, blobs, result = verdict(files, scopes, declared, problems, missing)
        rows[key] = {"evidence": evidence, "blobs": {digest: data.decode("utf-8") for digest, data in blobs.items()},
                     "allow": allows}
        eligible = [lic.eligibility(result, allow) for allow in allows]
        results[key] = {"verdict": {"form": result.form, "expression": result.expression, "ids": list(result.ids),
                                    "strict": result.strict, "problems": list(result.problems)},
                        "eligibility": [{"status": item.status, "usable_under": item.usable_under,
                                         "policy": list(item.policy), "reason": item.reason} for item in eligible]}
    cases.append(Case("eligibility", ok(results), {"calls": rows}, function="oah.design.sources.license.eligibility",
                      note="a table: verdict(evidence, blobs) once per row, then eligibility under each allow list "
                           "in `allow`, in order"))

    rows, results = {}, {}
    for key, name, data in (("cargo", "Cargo.toml", b'[package]\nname = "x"\nlicense = "MIT OR Apache-2.0"\n'),
                            ("package-json", "package.json", b'{"license": "MIT"}'),
                            ("pyproject-table", "pyproject.toml", b'[project]\nlicense = {text = "ISC"}\n'),
                            ("package-json-no-field", "package.json", b'{"name": "x"}'),
                            ("cargo-invalid", "Cargo.toml", b"[package\n")):
        declared, problem = lic.declared_from_manifest(name, data)
        rows[key] = {"path": name, "text": data.decode("utf-8")}
        results[key] = {"declared": declared, "problem": problem}
    cases.append(Case("declared", ok(results), {"calls": rows},
                      function="oah.design.sources.license.declared_from_manifest",
                      note="a table: each row passes the manifest path and its UTF-8 bytes"))

    return Area(
        "license",
        functions=[fn("oah.design.sources.license", "classify_text"),
                   fn("oah.design.sources.license", "parse_expression"),
                   fn("oah.design.sources.license", "declared_from_manifest"),
                   fn("oah.design.sources.license", "verdict"), fn("oah.design.sources.license", "eligibility")],
        mined_from=["tests/test_design_sources.py"],
        cases=cases,
        omitted=[
            {"behaviour": "license discovery over a fetched repository tree (discover)",
             "reason": "it walks a collector snapshot; the review records pin it end to end through review()"},
            {"behaviour": "a manifest over MANIFEST_LIMIT bytes",
             "reason": "a 256 KiB input for one size check; the limit constant is in the pinned source"},
            {"behaviour": "copyright notices that carry an e-mail address",
             "reason": "fixtures hold no e-mail address (privacy rule); the holder rules are pinned without it"},
        ],
        notes=["Table cases give `calls` (row name -> arguments) and expect a map of row name -> result.",
               "eligibility rows: `evidence` and `blobs` (sha256 -> text) are what verdict() reads; a listed file "
               "whose digest is absent from `blobs` is a missing stored text.",
               "The Apache-2.0 text is the terms section of the copy tag v0.4.0 ships in skills/skill-creator."],
    )
