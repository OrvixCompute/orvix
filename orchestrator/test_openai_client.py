"""Call the Orvix inference API using the official OpenAI Python client.

Requires `pip install openai`. Run the server first, then:

    python test_openai_client.py

Uses the seeded test API key by default (see migrations/001_initial_schema.sql).
"""

import os

from openai import OpenAI

API_KEY = os.environ.get("ORVIX_API_KEY", "orvx_sk_testkey0testkey0testkey0testkey0")

client = OpenAI(base_url="http://localhost:8000/v1", api_key=API_KEY)


def non_streaming() -> None:
    print("=== non-streaming ===")
    resp = client.chat.completions.create(
        model="qwen-2.5-7b",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Explain the Orvix network in one sentence."},
        ],
        max_tokens=128,
    )
    print("content:", resp.choices[0].message.content)
    print("usage:", resp.usage)


def streaming() -> None:
    print("\n=== streaming ===")
    stream = client.chat.completions.create(
        model="mistral-7b",
        messages=[{"role": "user", "content": "Stream me a greeting."}],
        max_tokens=128,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
    print()


if __name__ == "__main__":
    non_streaming()
    streaming()
