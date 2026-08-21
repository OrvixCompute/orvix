#!/usr/bin/env bash
# Build the staking program for Solana SBF and stage the artifact where
# `anchor deploy` expects it (target/deploy/orvix_staking.so).
#
# Uses the official cargo-build-sbf from the 3.1.10 solana release, which
# bundles a rustc (1.89) new enough for the current dependency tree and
# produces a binary the devnet verifier (Agave 4.x) accepts.
set -euo pipefail

cd "$(dirname "$0")"

CARGO_BUILD_SBF=~/.local/share/solana/install/releases/3.1.10/solana-release/bin/cargo-build-sbf
if [ ! -x "$CARGO_BUILD_SBF" ]; then
    echo "error: cargo-build-sbf (3.1.10) not found at $CARGO_BUILD_SBF" >&2
    echo "Install it with: agave-install init 3.1.10" >&2
    exit 1
fi

# cargo-build-sbf stages a stripped copy at target/deploy/orvix_staking.so itself.
"$CARGO_BUILD_SBF" --manifest-path programs/staking/Cargo.toml

echo "Staged target/deploy/orvix_staking.so"
echo "Program ID: $(solana-keygen pubkey target/deploy/orvix_staking-keypair.json)"
