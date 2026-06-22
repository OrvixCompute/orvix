"""End-to-end auth test: generates a throwaway Solana keypair, requests a
challenge, signs it, and verifies — no real Phantom wallet needed.

Run the server first (`uvicorn app.main:app --reload`), then:

    python test_auth.py
"""

import sys

import httpx
from solders.keypair import Keypair

BASE_URL = "http://localhost:8000"


def main() -> int:
    # 1. Generate a throwaway wallet (acts as the user's Phantom wallet).
    kp = Keypair()
    wallet = str(kp.pubkey())
    print(f"Test wallet: {wallet}")

    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        # 2. Request a challenge.
        r = client.get("/v1/auth/challenge", params={"wallet": wallet})
        r.raise_for_status()
        challenge = r.json()
        message = challenge["message"]
        print("\nChallenge message:\n" + message)

        # 3. Sign the exact message bytes with the wallet's private key.
        signature = kp.sign_message(message.encode("utf-8"))
        sig_b58 = str(signature)  # solders renders signatures as base58
        print(f"\nSignature (base58): {sig_b58}")

        # 4. Verify.
        r = client.post(
            "/v1/auth/verify",
            json={"wallet": wallet, "message": message, "signature": sig_b58},
        )
        if r.status_code != 200:
            print(f"\nVerify FAILED ({r.status_code}): {r.text}")
            return 1
        data = r.json()
        token = data["token"]
        print("\nVerify OK. User:", data["user"])
        print(f"JWT: {token[:40]}...")

        # 5. Use the JWT against a protected endpoint.
        r = client.post("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        print("\n/v1/auth/me ->", r.json())

    print("\nAuth flow succeeded ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
