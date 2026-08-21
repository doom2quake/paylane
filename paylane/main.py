"""PayLane CLI: a reserve-gated Solana stablecoin rail.

    paylane state                     # show reserves, circulating, headroom
    paylane attest 1000000            # attest on-chain reserves (attestor)
    paylane mint 400000               # mint (gated: cannot exceed reserves)
    paylane settle 100000             # redeem out of circulation
    paylane reset                     # start a fresh treasury
    paylane demo                      # the whole hero story in one run
    paylane --help

Commands share one treasury: state is persisted to `.paylane-state.json` (or
`--state PATH`) between invocations, so `attest`, then `mint`, then `state` form
a real workflow instead of three unrelated treasuries.

The hero: the treasury program provably cannot mint past on-chain-attested
reserves. Ask for one unit more than the reserves back and the program rejects it
deterministically. Report a reserve loss and it pauses itself.

This CLI drives the OFFLINE mirror of the on-chain treasury, keyless. Nothing is
submitted to a cluster and events are labelled `sim:`; the on-chain program's own
behaviour is proven by `cargo test` (`programs/paylane/tests/program.rs`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .agent import PaylaneAgent
from .treasury import Treasury

# 6-decimal token: 1_000_000 base units == 1.00 USD.
_DEC = 1_000_000

DEFAULT_STATE_PATH = ".paylane-state.json"


def _pace() -> None:
    """Optional pause between demo beats for a legible screen recording.
    Off by default (PAYLANE_PACE unset/0) so tests and normal runs are instant."""
    try:
        d = float(os.getenv("PAYLANE_PACE", "0"))
    except ValueError:
        d = 0.0
    if d > 0:
        time.sleep(d)


# --- tiny ANSI palette (the terminal is part of the demo) --------------------
_C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[36m",
      "m": "\033[35m", "d": "\033[2m", "bold": "\033[1m", "x": "\033[0m"}


def _p(s: str = "") -> None:
    print(s)


def _kv(k: str, v: str, color: str = "b") -> None:
    print(f"  {_C['d']}{k:<16}{_C['x']} {_C[color]}{v}{_C['x']}")


def _rule(title: str = "") -> None:
    print(f"{_C['d']}{'-' * 66}{_C['x']}" + (f" {_C['bold']}{title}{_C['x']}" if title else ""))


def _usd(units: int) -> str:
    return f"${units / _DEC:,.2f}"


# --- persistence -------------------------------------------------------------

def load_agent(path: Path) -> PaylaneAgent:
    """Resume the treasury from `path`, or start a fresh one if it is absent."""
    if path.exists():
        snap = json.loads(path.read_text())
        return PaylaneAgent(Treasury.restore(snap))
    return PaylaneAgent()


def save_agent(agent: PaylaneAgent, path: Path) -> None:
    path.write_text(json.dumps(agent.treasury.snapshot(), indent=2))


def _show_state(agent: PaylaneAgent) -> None:
    s = agent.state()
    _kv("attested reserve", _usd(s["attested_reserve"]), "g")
    _kv("circulating", _usd(s["circulating"]), "b")
    _kv("mint headroom", _usd(s["available_to_mint"]), "m")
    bps = s["backing_bps"]
    ratio = "no supply" if s["circulating"] == 0 else f"{bps / 100:.1f}%"
    _kv("backing", ratio, "g" if not s["paused"] else "r")
    if s["paused"]:
        _kv("status", "PAUSED - undercollateralized, minting blocked", "r")


def cmd_state(agent: PaylaneAgent) -> None:
    _rule("treasury state")
    _show_state(agent)


def _report(res: dict[str, Any]) -> None:
    kind = res.get("kind")
    if res["status"] == "blocked":
        _p(f"{_C['y']}rate-limited:{_C['x']} {res['reason']}")
        return
    ev = res["event"]
    if res["status"] == "rejected":
        _p(f"  {_C['r']}REJECTED{_C['x']} {kind} {_usd(res['amount'])}  "
           f"{_C['d']}({ev['reason']}){_C['x']}")
        _p(f"  {_C['d']}no transaction was produced; treasury state is unchanged{_C['x']}")
    else:
        verb = {"mint": "minted", "settle": "settled", "attest": "attested"}.get(kind, kind)
        tag = "offline mirror, no tx" if ev["simulated"] else "on-chain"
        _p(f"  {_C['g']}OK{_C['x']} {verb} {_usd(res['amount'])}  "
           f"{_C['d']}{tag}{_C['x']} {_C['b']}{ev['signature'][:24]}{_C['x']}")
        if res["paused"]:
            _p(f"  {_C['r']}TREASURY PAUSED{_C['x']} {_C['d']}reserves are below "
               f"circulating supply; minting is blocked{_C['x']}")
    _kv("circulating", _usd(res["circulating"]))
    _kv("attested", _usd(res["attested_reserve"]), "g")
    _kv("headroom", _usd(res["available_to_mint"]), "m")


def cmd_mint(agent: PaylaneAgent, amount: int) -> dict[str, Any]:
    res = agent.mint(amount)
    _report(res)
    return res


def cmd_settle(agent: PaylaneAgent, amount: int) -> dict[str, Any]:
    res = agent.settle(amount)
    _report(res)
    return res


def cmd_attest(agent: PaylaneAgent, amount: int) -> dict[str, Any]:
    res = agent.attest(amount)
    _report(res)
    return res


def cmd_demo(agent: PaylaneAgent) -> None:
    _p(f"{_C['bold']}PayLane{_C['x']} - reserve-gated stablecoin treasury on Solana  "
       f"{_C['d']}(offline mirror; the program itself is tested in cargo test){_C['x']}")
    _p(f"{_C['d']}the treasury program provably cannot mint past on-chain-attested reserves{_C['x']}")
    _p()

    _p(f"{_C['bold']}1. The attestor posts proof-of-reserve on-chain: $1,000,000 backing.{_C['x']}")
    cmd_attest(agent, 1_000_000 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}2. The rail mints within reserves. Two mints, $800,000 total. Allowed.{_C['x']}")
    cmd_mint(agent, 300_000 * _DEC)
    _pace()
    cmd_mint(agent, 500_000 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}3. Now try to mint $300,000 more. That would be $1.1M against $1.0M backing.{_C['x']}")
    _p(f"{_C['d']}   The program runs the reserve gate and refuses. No admin can override it.{_C['x']}")
    cmd_mint(agent, 300_000 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}4. Settle $100,000 out of circulation, which frees mint headroom.{_C['x']}")
    _p(f"{_C['d']}   On-chain this burns the holder's tokens, so supply really falls.{_C['x']}")
    cmd_settle(agent, 100_000 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}5. The same $300,000 mint now fits under the ceiling. Allowed.{_C['x']}")
    cmd_mint(agent, 300_000 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}6. Bad news: the reserves are audited at $600,000 against $1,000,000 circulating.{_C['x']}")
    _p(f"{_C['d']}   The lower figure is RECORDED, not refused, and the treasury pauses itself.{_C['x']}")
    cmd_attest(agent, 600_000 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}7. Any mint while undercollateralized is refused, however small.{_C['x']}")
    cmd_mint(agent, 1 * _DEC)
    _pace()
    _p()

    _p(f"{_C['bold']}8. Holders redeem $400,000. Circulating is back under the reserves, so the "
       f"treasury unpauses.{_C['x']}")
    cmd_settle(agent, 400_000 * _DEC)
    _pace()
    _p()

    _rule("final treasury state")
    _show_state(agent)
    _p()
    _p(f"{_C['g']}Circulating supply can never exceed attested reserves, and a reserve loss "
       f"stops the mint instead of hiding. Enforced by the program, not by trust.{_C['x']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paylane", description="Reserve-gated Solana stablecoin rail.")
    p.add_argument("--state", default=DEFAULT_STATE_PATH,
                   help=f"treasury state file shared between commands (default {DEFAULT_STATE_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state", help="show reserves, circulating supply, and mint headroom")
    a = sub.add_parser("attest", help="attest on-chain reserves (in base units)")
    a.add_argument("amount", type=int)
    m = sub.add_parser("mint", help="mint stablecoin (reserve-gated)")
    m.add_argument("amount", type=int)
    s = sub.add_parser("settle", help="redeem stablecoin out of circulation")
    s.add_argument("amount", type=int)
    sub.add_parser("reset", help="delete the saved treasury and start empty")
    sub.add_parser("demo", help="run the whole hero story (attest -> mint -> reject -> loss -> pause) in one run")
    return p


def cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.state)

    if args.cmd == "reset":
        if path.exists():
            path.unlink()
        _p(f"{_C['d']}treasury reset ({path}){_C['x']}")
        return 0

    # `demo` tells one story from zero; everything else continues the workflow.
    agent = PaylaneAgent() if args.cmd == "demo" else load_agent(path)

    if args.cmd == "state":
        cmd_state(agent)
    elif args.cmd == "attest":
        cmd_attest(agent, args.amount)
    elif args.cmd == "mint":
        cmd_mint(agent, args.amount)
    elif args.cmd == "settle":
        cmd_settle(agent, args.amount)
    elif args.cmd == "demo":
        cmd_demo(agent)

    save_agent(agent, path)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
