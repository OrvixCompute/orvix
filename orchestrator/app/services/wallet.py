"""Treasury keypair loading for admin on-chain operations (buyback, burn).

The keypair file is a JSON array of bytes (the format produced by
`solana-keygen` and Phantom export). It must be readable only by the service
user (chmod 600). Loaded lazily so importing this module never touches disk.
"""

import json

from solders.keypair import Keypair


def load_keypair(path: str) -> Keypair:
    """Load a Solana keypair from a JSON byte-array file."""
    if not path:
        raise ValueError("No keypair path configured (set TREASURY_KEYPAIR_PATH)")
    with open(path) as f:
        secret = json.load(f)
    return Keypair.from_bytes(bytes(secret))
