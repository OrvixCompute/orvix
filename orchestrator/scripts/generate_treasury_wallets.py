"""Generate the 3 treasury keypairs (main cold, hot, payout). RUN LOCALLY ONLY.

Outputs JSON byte-array keypair files (the solana-keygen format load_keypair reads)
into ./treasury-keys/ and prints the public keys.

Provisioning after running:
- main:   write the file's byte array to PAPER, then DELETE the file. The main
          private key must NEVER live on the server (cold/offline storage).
- hot:    SCP to the VPS, `chmod 600`, point TREASURY_KEYPAIR_PATH at it.
- payout: SCP to the VPS, `chmod 600`, point PAYOUT_KEYPAIR_PATH at it.
- Put all 3 public keys into .env (TREASURY_MAIN_PUBLIC / TREASURY_WALLET_ADDRESS /
  PAYOUT_WALLET_PUBLIC) and into the treasury_wallets table.

Do NOT run this on the VPS via chat — generate locally, move keys deliberately.
"""

import json
import sys
from pathlib import Path

from solders.keypair import Keypair

OUT_DIR = Path("treasury-keys")


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    for role in ("main", "hot", "payout"):
        kp = Keypair()
        path = OUT_DIR / f"{role}.json"
        path.write_text(json.dumps(list(bytes(kp))))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        print(f"{role:7} pubkey = {kp.pubkey()}   file = {path}")
    print(
        "\nNEXT:\n"
        "  main   -> copy the byte array to PAPER, then DELETE treasury-keys/main.json\n"
        "  hot    -> SCP to VPS, chmod 600, set TREASURY_KEYPAIR_PATH\n"
        "  payout -> SCP to VPS, chmod 600, set PAYOUT_KEYPAIR_PATH\n"
        "  Set the 3 public keys in .env + treasury_wallets, then run create_treasury_atas.py."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
