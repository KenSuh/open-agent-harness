# Desktop release updates

The desktop app checks GitHub Releases on startup and periodically while open. It verifies the update
signature and the version bound to that signature before it runs an installer. The configured public key
belongs to the stable release channel. Keep its private key in the publisher's secure storage and retain
a protected backup. Keep it out of the source tree, CI, and release assets.

For a stable release, build the macOS DMG and Windows NSIS installer with the pinned installer workflow.
Download the combined installer artifact from the successful attempt, including its `SHA256SUMS` and
`SOURCE.txt`. From that checked-out commit on macOS, install the locked desktop build tools and prepare
the updater files with the stable signing key in the environment.

```sh
npm ci --prefix apps/desktop
node apps/desktop/scripts/prepare-updates.mjs /path/to/installer-artifacts FULL_SOURCE_SHA
```

The helper reads `TAURI_SIGNING_PRIVATE_KEY_PATH` and, for an encrypted key,
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. It compares the public key with the app configuration, checks both
installer digests, and archives the app from the DMG. It signs the macOS update archive and Windows EXE
for the same version and puts both signatures in `latest.json`.

Upload the `*.app.tar.gz` archive and `latest.json` alongside the installers in the GitHub release draft.
Include both in the published `SHA256SUMS`. Verify all assets and the source tag before publishing the
release as latest. The standalone signature files are already represented in `latest.json`.
