# Third-party notices

TCode embeds Cua Driver and `@trycua/cua-driver` 0.35.2 from the
[Cua project](https://github.com/trycua/cua), licensed under the MIT License.
The standalone driver’s original license and upstream notices are shipped in
`TCode.app/Contents/Resources/computer/` as `LICENSE` and `THIRD_PARTY_NOTICES.md`.

Its native Node runtime and UniFFI bindings include Mozilla Public License 2.0
components (`@trycua/cua-driver-darwin-arm64`, `@ubjs/core`, `@ubjs/node`, and
the matching platform runtime). The corresponding source and deterministic
runtime build are published in the Cua repository at tag
[`cua-driver-rs-v0.35.2`](https://github.com/trycua/cua/tree/cua-driver-rs-v0.35.2).
The matching `@ubjs` 0.31.0-3 source is at
[uniffi-bindgen-react-native, commit `dcb5c4a`](https://github.com/jhugman/uniffi-bindgen-react-native/tree/dcb5c4ab2350d57f6d26f5fa81a99c77ed86d449).
The MPL-2.0 text is available in `Contents/Resources/computer/MPL-2.0.txt`.
The native package’s `node-runtime-NOTICE.md` is bundled alongside the runtime.

MIT: <https://opensource.org/license/mit>

MPL 2.0: <https://www.mozilla.org/MPL/2.0/>
