// npm shim 시험: Windows 규칙에서 배치 파일을 선언한 패키지의 node 와 스크립트로 바꾸거나, 근거가 모자라면 거절하는지 본다.
use super::*;

/// A global npm layout under `root`: `tool.cmd` beside `node_modules/<package>` whose `bin` declares `tool`.
fn global_shim(root: &Path, package: &str, manifest: &str, script: &str) -> PathBuf {
    write(&root.join("tool.cmd"), "@echo off\r\n");
    write(
        &root.join("node_modules").join(package).join("package.json"),
        manifest,
    );
    let script = root.join("node_modules").join(package).join(script);
    write(&script, "console.log(1)\n");
    script
}

#[test]
fn an_npm_shim_runs_the_node_beside_it_with_the_declared_script() {
    let (_dir, root) = folder();
    let global = root.join("global");
    let manifest = r#"{"name": "tool-pkg", "bin": {"Tool": "./bin/tool.js"}}"#;
    let script = global_shim(&global, "tool-pkg", manifest, "bin/tool.js");
    write(&global.join("node.exe"), "MZ");
    write(&root.join("earlier/node.exe"), "MZ");
    let env = env_with_path(&[&root.join("earlier"), &global]);
    let node = global.join("node.exe");
    assert_eq!(
        resolve(&env, "tool", Rule::Windows),
        found(&[&node, &script], &global.join("tool.cmd"))
    );
}

#[test]
fn a_project_shim_reads_the_enclosing_node_modules_and_node_on_path() {
    let (_dir, root) = folder();
    let bin = root.join("project/node_modules/.bin");
    write(&bin.join("tool.cmd"), "@echo off\r\n");
    let package = root.join("project/node_modules/@scope/tool");
    write(
        &package.join("package.json"),
        r#"{"name": "@scope/tool", "bin": "cli.js"}"#,
    );
    write(&package.join("cli.js"), "console.log(1)\n");
    let other = root.join("project/node_modules/other");
    write(
        &other.join("package.json"),
        r#"{"name": "other", "bin": "cli.js"}"#,
    );
    write(&other.join("cli.js"), "console.log(2)\n");
    write(&root.join("node/node.exe"), "MZ");
    let env = env_with_path(&[&bin, &root.join("node")]);
    let expected = found(
        &[&root.join("node/node.exe"), &package.join("cli.js")],
        &bin.join("tool.cmd"),
    );
    assert_eq!(resolve(&env, "tool", Rule::Windows), expected);
}

#[test]
fn the_first_folder_wins_even_when_its_batch_file_is_refused() {
    let (_dir, root) = folder();
    write(&root.join("a/tool.cmd"), "@echo off\r\n");
    write(&root.join("b/tool.exe"), "MZ");
    let env = env_with_path(&[&root.join("a"), &root.join("b")]);
    let expected = unsupported(
        &root.join("a/tool.cmd"),
        "no installed npm package declares this command",
    );
    assert_eq!(resolve(&env, "tool", Rule::Windows), expected);
}

#[test]
fn two_declaring_packages_are_refused() {
    let (_dir, root) = folder();
    global_shim(&root, "one", r#"{"bin": {"tool": "a.js"}}"#, "a.js");
    write(
        &root.join("node_modules/two/package.json"),
        r#"{"bin": {"tool": "b.js"}}"#,
    );
    write(&root.join("node_modules/two/b.js"), "1\n");
    write(&root.join("node.exe"), "MZ");
    let env = env_with_path(&[&root]);
    let expected = unsupported(
        &root.join("tool.cmd"),
        "several npm packages declare this command",
    );
    assert_eq!(resolve(&env, "tool", Rule::Windows), expected);
}

#[test]
fn a_script_outside_node_modules_is_not_trusted() {
    let (_dir, root) = folder();
    global_shim(
        &root,
        "pkg",
        r#"{"bin": {"tool": "../../evil.js"}}"#,
        "unused.js",
    );
    write(&root.join("evil.js"), "1\n");
    write(&root.join("node.exe"), "MZ");
    let env = env_with_path(&[&root]);
    let expected = unsupported(
        &root.join("tool.cmd"),
        "no installed npm package declares this command",
    );
    assert_eq!(resolve(&env, "tool", Rule::Windows), expected);
}

#[test]
fn a_shim_without_node_exe_is_refused() {
    let (_dir, root) = folder();
    global_shim(&root, "pkg", r#"{"bin": {"tool": "cli.js"}}"#, "cli.js");
    let env = env_with_path(&[&root]);
    let expected = unsupported(
        &root.join("tool.cmd"),
        "node.exe was not found for this npm shim",
    );
    assert_eq!(resolve(&env, "tool", Rule::Windows), expected);
}
