use oah_core::{config, fsx, rules};

#[path = "rules/support.rs"]
mod support;

#[path = "rules/fixtures.rs"]
mod fixtures;

#[path = "rules/block.rs"]
mod block;

#[path = "rules/body.rs"]
mod body;

#[path = "rules/thin.rs"]
mod thin;

#[path = "rules/special.rs"]
mod special;

#[path = "rules/sync.rs"]
mod sync;
