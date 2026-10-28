"""C10.2 regression: metadata-URI SSRF allowlist.

The metadata URI is attacker-controlled (set by the token creator on-chain),
so `_safe_metadata_uri` must refuse everything except https/http with a
publicly-resolved host, and map the two legitimate native schemes (ipfs://,
ar://) to public gateways. Blocked targets: loopback, link-local cloud
metadata (169.254.169.254), RFC1918, CGNAT, reserved/unspecified, and any
non-http scheme.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import _safe_metadata_uri  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


print("verify_ssrf_allowlist.py: C10.2 metadata-URI SSRF allowlist")

# --- native schemes mapped to public gateways -----------------------------
check(
    "ipfs:// maps to ipfs.io gateway",
    _safe_metadata_uri("ipfs://QmWxYz123/logo.json")
    == "https://ipfs.io/ipfs/QmWxYz123/logo.json",
)
check(
    "ar:// maps to arweave.net gateway",
    _safe_metadata_uri("ar://abc123hash") == "https://arweave.net/abc123hash",
)
check(
    "ipfs:// strips leading slashes in the CID path",
    _safe_metadata_uri("ipfs:///QmX") == "https://ipfs.io/ipfs/QmX",
)

# --- non-http schemes refused before any DNS work ------------------------
check("file:// refused", _safe_metadata_uri("file:///etc/passwd") is None)
check("ftp:// refused", _safe_metadata_uri("ftp://example.com/x") is None)
check("gopher:// refused", _safe_metadata_uri("gopher://example.com") is None)
check("data: refused", _safe_metadata_uri("data:text/plain;base64,SGk=") is None)
check("empty string refused", _safe_metadata_uri("") is None)
check("hostless refused", _safe_metadata_uri("https:///no-host") is None)

# --- private / special addresses refused (attacker-controlled host) ------
check(
    "cloud metadata 169.254.169.254 refused",
    _safe_metadata_uri("http://169.254.169.254/latest/meta-data/") is None,
)
check("127.0.0.1 refused", _safe_metadata_uri("http://127.0.0.1:8080/admin") is None)
check("localhost refused", _safe_metadata_uri("http://localhost/x") is None)
check("::1 refused", _safe_metadata_uri("http://[::1]/x") is None)
check("RFC1918 10/8 refused", _safe_metadata_uri("http://10.0.0.5/x") is None)
check("RFC1918 192.168/16 refused", _safe_metadata_uri("http://192.168.1.10/x") is None)
check(
    "RFC1918 172.16/12 refused",
    _safe_metadata_uri("http://172.16.0.1/x") is None,
)
check("CGNAT 100.64/10 refused", _safe_metadata_uri("http://100.64.0.1/x") is None)
check("0.0.0.0 refused", _safe_metadata_uri("http://0.0.0.0/x") is None)

# --- public https/http allowed -------------------------------------------
check(
    "public https host passes",
    _safe_metadata_uri("https://example.com/meta.json")
    == "https://example.com/meta.json",
)
check(
    "public http host passes",
    _safe_metadata_uri("http://example.com/m.json") == "http://example.com/m.json",
)
check(
    "public host with port preserves port",
    _safe_metadata_uri("https://example.com:8443/m.json")
    == "https://example.com:8443/m.json",
)

print(f"\nverify_ssrf_allowlist.py: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)