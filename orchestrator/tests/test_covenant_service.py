"""Tests for the OpenCovenant MCP client (covenant_service.py).

The Covenant Guard endpoint is a remote HTTP stream (SSE). These tests never
touch the network — they exercise the JSON-RPC framing, the SSE line parsing,
and the structuredContent parsing against recorded real responses.
"""

from app.services.covenant_service import CovenantError, CovenantService


class FakeStream:
    """Minimal stand-in for httpx stream responses used by _request."""

    def __init__(self, frames: list[dict | None], is_error: bool = False):
        # frames: the list of `data:` payloads to emit; None -> a non-data line
        self._frames = frames
        self._is_error = is_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for f in self._frames:
            if f is None:
                yield ": keep-alive comment"
            else:
                import json

                yield f"data: {json.dumps(f)}"


def _svc(frames) -> CovenantService:
    svc = CovenantService(endpoint="https://test.invalid/mcp")

    async def fake_request(method, params):
        svc._id += 1
        # _request picks the frame matching svc._id
        resp = None
        for f in frames:
            if f is not None and f.get("id") == svc._id:
                resp = f
                break
        if resp is None:
            raise CovenantError("No response frame")
        if "error" in resp:
            raise CovenantError(f"MCP error: {resp['error']}")
        return resp.get("result")

    svc._request = fake_request
    svc._initialized = True  # skip the handshake
    return svc


REP_RESULT = {
    "content": [
        {
            "type": "text",
            "text": "Covenant reputation for 7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC\n"
            "score 0/1000 · bronze\n0 settled jobs · 0 distinct counterparties · $0 USDC inbound\n"
            "Grounded in public on-chain USDC settlements. Self-payments excluded.",
        }
    ],
    "structuredContent": {
        "wallet": "7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC",
        "settled_jobs": 0,
        "distinct_counterparties": 0,
        "volume_micro_usdc": 0,
        "tier": "bronze",
        "score": 0,
        "source_fee_payer": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4",
    },
}


async def test_check_reputation_parses_recorded_response():
    svc = _svc([{"jsonrpc": "2.0", "id": 1, "result": REP_RESULT}])
    result = await svc.check_reputation("7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC")

    assert result.ok is True
    assert result.reputation is not None
    rep = result.reputation
    assert rep.wallet == "7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC"
    assert rep.score == 0
    assert rep.tier == "bronze"
    assert rep.settled_jobs == 0
    assert rep.source_fee_payer == "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"
    assert rep.has_history is False


async def test_check_reputation_error_frame_returns_failed_result():
    svc = _svc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "asset lookup failed"}],
                    "isError": True,
                },
            }
        ]
    )
    result = await svc.check_reputation("abc")
    assert result.ok is False
    assert "asset lookup failed" in (result.error or "")


async def test_check_reputation_transport_error_is_fail_soft():
    svc = CovenantService(endpoint="https://test.invalid/mcp")
    svc._initialized = True

    async def boom(method, params):
        raise CovenantError("MCP transport error: connect failed")

    svc._request = boom
    result = await svc.check_reputation("abc")
    assert result.ok is False
    assert "connect failed" in (result.error or "")


async def test_check_reputation_malformed_payload_is_fail_soft():
    svc = _svc(
        [{"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"score": "not-an-int"}}}]
    )
    result = await svc.check_reputation("abc")
    assert result.ok is False
    assert "payload" in (result.error or "")


async def test_verify_attestation_passes_through_result():
    svc = _svc([{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "valid"}]}}])
    result = await svc.verify_attestation({"domain": "orvix.network"})
    assert result.ok is True
    assert result.tool == "covenant_verify"


async def test_json_rpc_framing_skips_non_matching_ids_and_comments():
    # Real server emits SSE comments and possibly other ids; _request must only
    # accept the frame whose id matches the request it sent.
    svc = _svc(
        [
            None,  # comment line
            {"jsonrpc": "2.0", "id": 999, "result": {"wrong": True}},
            {"jsonrpc": "2.0", "id": 1, "result": REP_RESULT},
        ]
    )
    result = await svc.check_reputation("abc")
    assert result.ok is True
