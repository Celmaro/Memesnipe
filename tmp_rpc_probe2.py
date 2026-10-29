import sys, asyncio, time

sys.path.insert(0, "src")
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.core import TokenAccountOptsModel

TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

CANDIDATES = [
    ("zan.top", "https://api.zan.top/public/solana-mainnet"),
    ("mainnet-beta", "https://api.mainnet-beta.solana.com"),
]


async def test(name, url):
    c = AsyncClient(url, timeout=10)
    print(f"\n=== {name} getTokenAccountsByOwner (sweeper method) ===")
    owner = Pubkey.from_string("77ZumNZxMReNpuACvrphX5eK6o3UXjcSmXWx69SDJYQA")
    for prog, label in [(TOKEN, "Tokenkeg"), (TOKEN_2022, "TokenzQd")]:
        t0 = time.perf_counter()
        try:
            res = await c.get_token_accounts_by_owner(
                owner, TokenAccountOptsModel(program_id=prog)
            )
            dt = (time.perf_counter() - t0) * 1000
            n = len(res.value) if res.value else 0
            print(f"  {label}: {dt:.0f}ms ok n={n}")
        except Exception as e:
            dt = (time.perf_counter() - t0) * 1000
            print(f"  {label}: FAIL {type(e).__name__}: {str(e)[:100]} ({dt:.0f}ms)")
    await c.close()


async def main():
    for name, url in CANDIDATES:
        await test(name, url)


asyncio.run(main())