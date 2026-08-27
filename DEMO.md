# PayLane - demo shot list (2 min)

**Hero:** trying to mint one dollar past the on-chain-attested reserves makes the program refuse, deterministically, with no admin override. Reporting a real reserve loss makes it pause itself.

1. **(0:00) Open the UI.** "This is a Solana stablecoin rail. The treasury program cannot mint past the reserves attested on-chain, and circulating supply is read from the SPL mint, not from a number the program stores." Point at the reserve meter: circulating $800,000, ceiling $1,000,000. Note the badge: this page is a simulation of the program's rules, nothing is submitted.
2. **(0:20) Mint within reserves.** Slider at $100,000, click Mint. The fill bar grows, a MINT row appears in the event history. Solvent.
3. **(0:40) The wow: over-mint.** Click "Try to over-mint (the wow)" (a $1.3M mint against $1.0M backing). The verdict flips red: **REJECTED - would exceed attested reserves. No tx produced.** The history logs a REJECT with "no tx". State does not move.
4. **(1:00) The second wow: a reserve loss.** Click "Report a reserve loss" ($600k attested against $1M circulating). The lower figure is **recorded**, not refused, and the treasury **pauses**. Every mint is now refused, however small. "Hiding the loss would leave a stale, higher number on the books while the issuer kept minting."
5. **(1:20) Redeem back under the line.** Click Settle a few times. Once circulating falls under $600k the pause lifts by itself.
6. **(1:35) The proof.** `cargo test`. Point at `programs/paylane/tests/program.rs`: thirteen tests drive the real instructions against the **real SPL Token program** in an in-process bank and assert on `Mint.supply` and token balances. "A stubbed mint helper fails every one of these."
7. **(1:50) CLI.** `paylane demo` - the same story keyless in the terminal, state shared between commands.
8. **(2:00) Close.** "Solvency here is enforced by the rail, not promised by the operator. The program is not deployed yet, and we say so in the README; what you just saw is the program's own behaviour against the real token program."

Commands:
```
paylane demo
paylane attest 1000000000 && paylane mint 400000000 && paylane state
cargo test
PYTHONPATH=. python -m pytest -q
```
