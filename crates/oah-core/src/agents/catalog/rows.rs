// v0.4.0 도구 행: 14개 도구의 이름·필요 정도·목적·버전 하한·로그인 명령·시스템별 설치 안내를 v0.4.0 표시 순서대로 둔다.
//! The rows of [`super::Tool`] (v0.4:src/oah/setup/tools.py:117-174, v0.4:src/oah/setup/companions.py:55-80).

use super::{Guide, Requirement, StatusCommand, Tool};

/// The fields most rows leave at their default.
const ROW: Tool = Tool {
    name: "",
    requirement: Requirement::Recommended,
    purpose: "",
    min_version: None,
    required_for_gates: false,
    sign_in: None,
    macos: Guide {
        install: &[],
        url: "",
    },
    windows: Guide {
        install: &[],
        url: "",
    },
};

const fn both(
    macos: &'static [&'static str],
    windows: &'static [&'static str],
    url: &'static str,
) -> [Guide; 2] {
    [
        Guide {
            install: macos,
            url,
        },
        Guide {
            install: windows,
            url,
        },
    ]
}

const UNLAZY_NOTE: &str = "Run oah init --with-unlazy after lane P5 lands to install unlazy at P5's \
    contract-tested pin, verified by commit and scripts digest. This guided setup does not install it.";
const ASIDE_WINDOWS_NOTE: &str = "Download the official installer from the page below and run it. The aside \
    command comes from Aside's CLI installer; python -m oah.setup.companions offers it.";

/// Every v0.4.0 tool, in v0.4.0's display order.
pub fn catalog() -> [Tool; 14] {
    let [git_mac, git_win] = [
        Guide {
            install: &["brew install git", "xcode-select --install"],
            url: "https://git-scm.com/install/mac",
        },
        Guide {
            install: &["winget install --id Git.Git -e --source winget"],
            url: "https://git-scm.com/install/windows",
        },
    ];
    let [python_mac, python_win] = both(
        &["uv python install 3.13", "brew install python@3.13"],
        &[
            "uv python install 3.13",
            "winget install Python.Python.3.13",
        ],
        "https://www.python.org/downloads/",
    );
    let [uv_mac, uv_win] = both(
        &["curl -LsSf https://astral.sh/uv/install.sh | sh"],
        &["irm https://astral.sh/uv/install.ps1 | iex"],
        "https://docs.astral.sh/uv/getting-started/installation/",
    );
    let [node_mac, node_win] = both(
        &["brew install node"],
        &["winget install OpenJS.NodeJS.LTS"],
        "https://nodejs.org/en/download",
    );
    let [claude_mac, claude_win] = both(
        &["curl -fsSL https://claude.ai/install.sh | bash"],
        &[
            "irm https://claude.ai/install.ps1 | iex",
            "winget install Anthropic.ClaudeCode",
        ],
        "https://code.claude.com/docs/en/setup",
    );
    let [codex_mac, codex_win] = both(
        &["brew install --cask codex", "npm install -g @openai/codex"],
        &["irm https://chatgpt.com/codex/install.ps1 | iex"],
        "https://github.com/openai/codex",
    );
    let [unlazy_mac, unlazy_win] = both(
        &[UNLAZY_NOTE],
        &[UNLAZY_NOTE],
        "https://github.com/Leonxlnx/unlazy",
    );
    let [graphify_mac, graphify_win] = both(
        &["uv tool install graphifyy"],
        &["uv tool install graphifyy"],
        "https://docs.graphify.com/installation",
    );
    let [gh_mac, gh_win] = both(
        &["brew install gh"],
        &["winget install --id GitHub.cli --source winget"],
        "https://cli.github.com",
    );
    let [gitleaks_mac, gitleaks_win] = both(
        &["brew install gitleaks"],
        &["scoop install gitleaks"],
        "https://github.com/gitleaks/gitleaks",
    );
    let [jq_mac, jq_win] = both(
        &["brew install jq"],
        &["winget install jqlang.jq"],
        "https://jqlang.org/download/",
    );
    let [rg_mac, rg_win] = both(
        &["brew install ripgrep"],
        &["winget install BurntSushi.ripgrep.MSVC"],
        "https://github.com/BurntSushi/ripgrep#installation",
    );
    let [orca_mac, orca_win] = both(
        &["brew install --cask stablyai/orca/orca"],
        &["winget install --id StablyAI.Orca -e"],
        "https://www.onorca.dev/download",
    );
    let [aside_mac, aside_win] = [
        Guide {
            install: &["brew install --cask aside"],
            url: "https://aside.com/download?os=mac",
        },
        Guide {
            install: &[ASIDE_WINDOWS_NOTE],
            url: "https://aside.com/download?os=win",
        },
    ];
    [
        Tool {
            name: "git",
            requirement: Requirement::Required,
            purpose: "version control",
            macos: git_mac,
            windows: git_win,
            ..ROW
        },
        Tool {
            name: "python",
            requirement: Requirement::Required,
            purpose: "runs oah",
            macos: python_mac,
            windows: python_win,
            ..ROW
        },
        Tool {
            name: "uv",
            purpose: "installs oah, graphify and Python",
            macos: uv_mac,
            windows: uv_win,
            ..ROW
        },
        Tool {
            name: "node",
            purpose: "runs unlazy's gate checks",
            min_version: Some([16, 0, 0]),
            required_for_gates: true,
            macos: node_mac,
            windows: node_win,
            ..ROW
        },
        Tool {
            name: "claude",
            requirement: Requirement::Required,
            purpose: "initial implementation (Claude Code)",
            sign_in: Some(StatusCommand {
                args: &["auth", "status"],
                login: "claude auth login",
            }),
            macos: claude_mac,
            windows: claude_win,
            ..ROW
        },
        Tool {
            name: "codex",
            requirement: Requirement::Required,
            purpose: "review and correction (Codex CLI)",
            sign_in: Some(StatusCommand {
                args: &["login", "status"],
                login: "codex login",
            }),
            macos: codex_mac,
            windows: codex_win,
            ..ROW
        },
        Tool {
            name: "unlazy",
            purpose: "gate checks and the Stop hook",
            macos: unlazy_mac,
            windows: unlazy_win,
            ..ROW
        },
        Tool {
            name: "graphify",
            purpose: "knowledge graph",
            macos: graphify_mac,
            windows: graphify_win,
            ..ROW
        },
        Tool {
            name: "gh",
            purpose: "pull requests from the terminal",
            macos: gh_mac,
            windows: gh_win,
            ..ROW
        },
        Tool {
            name: "gitleaks",
            purpose: "secret scan before push",
            macos: gitleaks_mac,
            windows: gitleaks_win,
            ..ROW
        },
        Tool {
            name: "jq",
            purpose: "reads JSON output in scripts",
            macos: jq_mac,
            windows: jq_win,
            ..ROW
        },
        Tool {
            name: "rg",
            purpose: "fast code search (ripgrep)",
            macos: rg_mac,
            windows: rg_win,
            ..ROW
        },
        Tool {
            name: "orca",
            requirement: Requirement::Optional,
            purpose: "optional app: runs coding agents side by side in isolated git worktrees",
            macos: orca_mac,
            windows: orca_win,
            ..ROW
        },
        Tool {
            name: "aside",
            requirement: Requirement::Optional,
            purpose: "optional app: a browser with an AI agent that Claude Code and Codex use for web tasks",
            macos: aside_mac,
            windows: aside_win,
            ..ROW
        },
    ]
}
