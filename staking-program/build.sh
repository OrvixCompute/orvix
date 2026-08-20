#!/usr/bin/env bash
# Build the staking program for the Solana SBF target and stage the artifact
# where `anchor deploy` expects it (target/deploy/orvix_staking.so).
#
# The bundled anchor CLI's cargo (1.79) is too old for the current dependency
# tree (solana-program 2.x needs Rust 1.85+), so we build with the Solana
# platform-tools toolchain (rustc 1.89 + sbpfv2 target) directly and copy the
# cdylib into place. `anchor deploy` then finds the artifact as usual.
set -euo pipefail

cd "$(dirname "$0")"

# Platform-tools v1.52 ships a self-contained rustc/cargo with SBF targets.
PT=~/.cache/solana/v1.52/platform-tools/rust/bin
if [ ! -x "$PT/cargo" ]; then
    echo "error: platform-tools not found at $PT" >&2
    echo "Install solana 2.1 (or point this script at your platform-tools)." >&2
    exit 1
fi

export PATH="$PT:$PATH"
export RUSTC="$PT/rustc"
# sbpfv1 keeps the ELF compatible with the classic loader on devnet/mainnet;
# sbpfv2 targets a newer runtime that may not be available everywhere yet.
TARGET=sbpfv1-solana-solana

cargo build --release --target "$TARGET"

mkdir -p target/deploy
cp "target/$TARGET/release/orvix_staking.so" target/deploy/orvix_staking.so

echo "Staged target/deploy/orvix_staking.so"
echo "Program ID: $(solana-keygen pubkey target/deploy/orvix_staking-keypair.json)"
