#!/usr/bin/env bash
# Build + deploy the staking program to devnet (or mainnet with --mainnet).
#
# Usage:
#   ./deploy.sh            # devnet
#   ./deploy.sh --mainnet  # mainnet-beta
set -euo pipefail

cd "$(dirname "$0")"

CLUSTER=devnet
if [ "${1:-}" = "--mainnet" ]; then
    CLUSTER=mainnet-beta
fi

./build.sh

PROGRAM_ID=$(solana-keygen pubkey target/deploy/orvix_staking-keypair.json)

# Deploy requires the upgrade-authority wallet to hold enough SOL for the
# buffer (roughly 2x the program rent). Check before paying fees.
NEEDED_SOL=1.0
BAL=$(solana balance --url "$CLUSTER" | awk '{print $1}')
echo "cluster=$CLUSTER program=$PROGRAM_ID balance=${BAL}SOL (need >= ${NEEDED_SOL}SOL)"

if [ "$(echo "$BAL >= $NEEDED_SOL" | bc)" != "1" ]; then
    echo "error: not enough SOL to deploy. Airdrop or fund the wallet first:" >&2
    echo "  solana airdrop 2 --url $CLUSTER" >&2
    exit 1
fi

anchor deploy --program-name orvix_staking --provider.cluster "$CLUSTER"

echo "Deployed $PROGRAM_ID"
solana program show "$PROGRAM_ID" --url "$CLUSTER" | head -8
