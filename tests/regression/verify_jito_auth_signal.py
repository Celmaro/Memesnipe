"""P2-STRUC-4 regression: Jito auth-keypair rotation + accepted-signal authority.

Reference behavior (reference bot's jito.rs): the block engine's
accepted/rejected answer is the authoritative landing verdict (no second RPC
confirm), and per-region auth keypairs rotate across requests. Guards:
1. sendBundle response classification: bundleId -> accepted; null/error ->
   rejected; malformed -> unknown (falls back to RPC confirm).
2. Auth headers sign the exact request body and rotate round-robin.
3. The trader wires auth_keypairs config into the Jito executor only when
   jito.enabled (off by default).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from execution.executor import JitoExecutor

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    from solders.keypair import Keypair

    # 1. Accepted: bundleId string -> "accepted" (authoritative, no RPC).
    check(
        "bundleId accepted",
        JitoExecutor.classify_send_bundle_response(
            {"jsonrpc": "2.0", "result": "abc123def"}
        )
        == "accepted",
    )
    # 2. Rejected: null result.
    check(
        "null result rejected",
        JitoExecutor.classify_send_bundle_response(
            {"jsonrpc": "2.0", "result": None}
        )
        == "rejected",
    )
    # 3. Rejected: error payload.
    check(
        "error payload rejected",
        JitoExecutor.classify_send_bundle_response(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "tip too low"}}
        )
        == "rejected",
    )
    # 4. Unknown: malformed.
    check(
        "malformed unknown",
        JitoExecutor.classify_send_bundle_response({"bogus": 1}) == "unknown",
    )
    check(
        "empty string result unknown",
        JitoExecutor.classify_send_bundle_response({"result": ""}) == "unknown",
    )

    # 5. Auth headers: sign exact body bytes, carry pubkey + signature.
    j = JitoExecutor.__new__(JitoExecutor)
    j.auth_keypairs = [Keypair()]
    j._auth_round_robin = 0  # noqa: SLF001
    body = {"jsonrpc": "2.0", "method": "sendBundle", "params": [["AAA"]]}
    h1 = j._auth_headers(body)
    check("signing key header", "x-jito-signing-key" in h1)
    check(
        "signing key is the auth pubkey",
        h1["x-jito-signing-key"] == str(j.auth_keypairs[0].pubkey()),
    )
    check("signature header", "Jito-Signature" in h1)
    # Signature verifies against the auth pubkey over the exact JSON body.
    from solders.signature import Signature

    sig = Signature.from_string(h1["Jito-Signature"])
    import json as _json

    body_bytes = _json.dumps(body, separators=(",", ":")).encode()
    check(
        "signature verifies over body bytes",
        sig.verify(j.auth_keypairs[0].pubkey(), body_bytes),
    )

    # 6. Rotation: two keypairs alternate round-robin.
    j2 = JitoExecutor.__new__(JitoExecutor)
    k1, k2 = Keypair(), Keypair()
    j2.auth_keypairs = [k1, k2]
    j2._auth_round_robin = 0  # noqa: SLF001
    p1 = j2._auth_headers(body)["x-jito-signing-key"]
    p2 = j2._auth_headers(body)["x-jito-signing-key"]
    p3 = j2._auth_headers(body)["x-jito-signing-key"]
    check(
        "keys rotate round-robin",
        p1 == str(k1.pubkey()) and p2 == str(k2.pubkey()) and p3 == str(k1.pubkey()),
        f"got {p1} {p2} {p3}",
    )

    # 7. No auth keypairs -> unsigned (empty headers).
    j3 = JitoExecutor.__new__(JitoExecutor)
    j3.auth_keypairs = []
    check("no keys -> unsigned", j3._auth_headers(body) == {})

    # 8. Config wiring: YAML declares auth_keypairs, default empty.
    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML declares jito.auth_keypairs",
        "auth_keypairs" in cfg.get("jito", {}),
    )
    check(
        "YAML default empty",
        cfg["jito"].get("auth_keypairs") == [],
    )

    print()
    if FAILURES:
        print(f"P2-STRUC-4 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-STRUC-4 PASSED: Jito auth rotation + accepted-signal verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())