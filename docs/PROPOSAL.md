# PayLane: an on-chain proof-of-reserves settlement gate for Solana stablecoins

**Applicant:** doom2quake (builder collective)
**Programme:** Solana Foundation Grants (standard, non-dilutive, milestone-based), with Superteam as the regional route
**Application channel:** the Solana Foundation funding application form linked from solana.org/grants
**Requested:** a milestone-based grant to take our tested treasury program from a toolchain-free test bench to a deployed, explorer-verifiable devnet program with an SDK and a public demo. We will size the ask to the milestone plan below during the intake call rather than name a headline figure here.
**Network target:** Solana devnet and testnet only. No mainnet, no real funds.
**Status of this document:** draft grant application. It is not an accepted, funded, or endorsed grant, and nothing here should be read as one.

---

## 0. Grant verification (read this first)

**Status: VERIFIED, with one INFERRED gating condition.**

What we verified against the official Solana pages ([solana.org/grants](https://solana.org/grants), [superteam.fun](https://superteam.fun/)) on 2026-08-24:

- There is a live grants program accepting applications on a rolling basis. Review is roughly one week, with a decision and contact stage over the following weeks.
- Two tracks exist: standard milestone-based grants for public goods (non-dilutive), and milestone-based convertible grants for public goods with a commercial component. We are applying to the standard, non-dilutive track.
- Eligibility is broad and explicitly includes individuals and independent teams. No incorporation is required to apply, and no residency requirement is stated on the general grants page. For a distributed collective this matters: the general Foundation grants program is not gated on a country of residence or a local legal entity the way some region-specific programs are.
- Superteam runs the regional route for parts of the ecosystem: local grants, bounties, and the connection into the Foundation's funding. A regional entry does not change the shape of the work, only the intake path.
- Funding is milestone-based. The page asks applicants to set clear, measurable funding milestones and a plan for how the money advances the work. It does not publish a fixed maximum for the standard track.
- What they fund: public goods that strengthen the network, with a strong open-source expectation and an "only possible on Solana" bar. Payments and stablecoin settlement infrastructure sit squarely inside that remit.

**The single gating check an operator must clear before applying:** the Foundation grant is disbursed under a signed grant agreement to a named counterparty, and there is a standard contracting and identity step attached to receiving funds. A distributed collective cannot sign as an abstract group. So the one thing to confirm before submitting is: at least one member can be the named grantee, sign the agreement, and pass the Foundation's counterparty checks, and we are willing to keep PayLane open-source as the program requires. If that holds, apply. If no member can be the named, contactable, contract-signing recipient, this moves to HOLD.

We are not relying on any region-specific residency, local-incorporation, or regional-KYC requirement for the general grants track. If the Foundation or Superteam routes us to a regional sub-program that adds such a requirement, we will re-verify against that program's own terms before proceeding.

---

## 1. The problem

Fiat-backed stablecoins settle on-chain but prove their solvency off-chain. The tokens move at the speed of the blockchain, while the promise that every token is backed lives in a bank statement and an issuer's word. Circulating supply is a public, on-chain number. The reserves behind it are not, and nothing on-chain stops an issuer from minting past the reserves that actually exist. When the two drift apart, holders find out late, usually during a redemption run, which is exactly the wrong moment.

The gap is structural. The mint authority is a key. Whoever holds it can mint. Reserve backing is a policy the issuer applies to themselves, off-chain, and asks everyone to trust. There is no on-chain object that refuses to mint the token that is not backed.

PayLane closes that gap for Solana stablecoins by making solvency a property the settlement program enforces, not a promise the issuer makes.

## 2. Why Solana, why now (Solana is load-bearing)

The guarantee only exists because of two Solana primitives working together, and it does not survive without them:

- **Circulating supply is read, never stored.** On Solana, the [SPL Token program](https://spl.solana.com/) keeps the authoritative `Mint.supply` for a mint. PayLane's treasury account deliberately does not store its own circulating figure. It reads `Mint.supply` from the bound mint on every instruction. That means the reserve check is always measured against the tokens that actually exist, not against a number the program wrote down about itself and hoped was still true. Remove this and you are back to a self-reported ledger that can drift.
- **The mint authority is held by a program-derived address, not a person.** At initialization the current mint authority hands the mint over to the treasury PDA in the same transaction. After that, the only code path that can mint is the program's own gate. There is no admin instruction that bypasses the reserve check, and no key a person can hold to mint around it.

Put together: on Solana we can make "you cannot mint past attested reserves" a program invariant enforced by a CPI into the real token program, with mint authority owned by a PDA. This is the "only possible on Solana" shape the Foundation looks for. It leans directly on SPL Token, on PDAs, and on cross-program invocation. It is not a generic idea we bolted a chain onto.

Why now: stablecoin settlement on Solana is growing, and the tooling gap is on the safety side. A reusable, tested, open-source reserve gate that any SPL mint can adopt is a public good the ecosystem does not yet have in a drop-in form.

## 3. What we have already built (honest evidence)

We are not starting from a slide. We have a working, tested program and an offline demo. Concretely, and only what the repository backs:

- **A load-bearing reserve invariant in pure Rust, unit-tested.** The gate lives in one file with no framework dependency, and it is tested on its own. The three rules it enforces: a mint cannot push `Mint.supply + amount` past the attested reserve (checked integer math, no float, no bypass); redemption only lowers supply by burning tokens the holder signs for; and an attestation below circulating supply is recorded as the truth and pauses minting rather than being rejected and leaving a stale, too-high figure on the books. The same file is compiled into the on-chain program and into a sibling toolchain-free crate so the invariant has one source of truth.
- **The program instructions tested against the real SPL Token program.** Using `solana-program-test`, the tests boot an in-process bank whose genesis contains the actual SPL Token binary. Every mint, burn, and authority transfer in those tests is a genuine CPI into that binary, and every assertion reads back the real `Mint.supply` and real token-account balances, not anything the program wrote about itself. A stubbed mint helper fails all of them. Mint authority really moves to the treasury PDA; a successful mint really moves supply and is re-read to prove it moved by exactly the gated amount; a rejected mint moves nothing; settle really burns; a recorded reserve loss really blocks the next mint.
- **Separated powers.** The key that attests reserves is required to differ from the key that mints against them, enforced at initialization.
- **An offline agent, a CLI with a one-command demo, and a self-contained UI.** The demo walks the full story: attest reserves, mint under the ceiling, get rejected one unit over, settle to free headroom, record a reserve loss and watch the treasury pause, then redeem back to solvency and unpause.

Verified counts, reproduced from a clean checkout:

- **`cargo test`: 52 Rust tests pass**, of which 13 run the program against the real SPL Token program in `solana-program-test` (`programs/paylane/tests/program.rs`) and the rest exercise the reserve invariant in both the program crate and the toolchain-free `reserve-core` crate.
- **`PYTHONPATH=. pytest -q`: 44 Python tests pass**, covering the offline agent, offline-model parity with the gate, the Anchor wire format (instruction discriminators and treasury account layout), and the fail-closed live seam.

What is honestly not done yet, stated plainly so a reviewer does not have to guess:

- The program is **not deployed.** Building the SBF artifact needs the Solana toolchain, which was not installed in our build environment, so there is currently no devnet program id, no funded keypair, and no explorer-verifiable transaction. The Anchor workspace is standard and `anchor build` is the missing step, but we have not run it and do not claim to have.
- The off-chain agent drives an **offline mirror** of the treasury and labels its events as simulated. It never emits a fake signature. What it does ship and test is the wire format a live client needs: the instruction discriminators and the exact treasury account layout.

This is the honest starting line. The grant is to cross it.

## 4. Milestones

Each milestone has a single deliverable and an acceptance test a reviewer can run or check. Milestone 1 is the first verifiable on-chain deliverable, because that is exactly the step we have not yet taken and the one that turns tested code into a real program.

**Milestone 1: Deployed, explorer-verifiable devnet program.**
Deliverable: build the SBF artifact with the Solana toolchain and deploy PayLane to Solana devnet, publishing the program id, the build hash, and a short transcript of a real initialize/attest/mint/settle sequence.
Acceptance test: a reviewer can open the published program id on a devnet explorer and see the deployed program plus the referenced transactions. A rejected over-the-ceiling mint appears as a failed transaction with the reserve error, not a silent success. All existing invariant and program tests still pass against the deployed program's logic.

**Milestone 2: Reserve-gate SDK and a live client that replaces the offline mirror.**
Deliverable: a small TypeScript client that constructs and sends the real instructions using the account layout and discriminators we already ship, so anyone can initialize a treasury for their own SPL mint, attest, mint, and settle on devnet without our offline agent.
Acceptance test: from a clean checkout, following the README, a developer can point the client at devnet, run the full attest/mint/reject/settle/pause/unpause sequence against the deployed program, and see each on-chain event. The client refuses to run against a treasury whose mint authority is not the PDA.

**Milestone 3: Attestation adapter and a worked reserve-oracle example.**
Deliverable: document and implement the attestor path end to end, with a reference attestor that posts a reserve figure on-chain on a schedule, plus a clear specification of how a real off-chain reserve source would sign attestations. This is the seam between a real bank/custody feed and the on-chain gate.
Acceptance test: a scheduled attestation lowers the recorded reserve below circulating supply on devnet, the treasury pauses, subsequent mints fail on-chain, and a later covering attestation or a redemption unpauses it, all visible on the explorer and reproducible from the docs.

**Milestone 4: Public demo, docs, and an adoption guide as a reusable public good.**
Deliverable: a hosted, self-contained demo UI wired to the devnet program, a written integration guide for issuers who want to put an existing SPL mint behind the reserve gate, the technical paper updated to match the deployed system, and the whole repository open-source under a permissive license.
Acceptance test: the demo drives real devnet transactions from the browser and links each to the explorer; the integration guide lets a third party stand up their own gated treasury on devnet by following it; the repository is public and the license is in place.

## 5. Impact on the Solana ecosystem

PayLane is infrastructure other builders reuse, which is the kind of public good the grants program is meant to fund:

- **A drop-in solvency gate for any SPL stablecoin.** The design binds one treasury PDA to one mint and enforces the reserve invariant through the standard SPL Token program, so an issuer does not need a bespoke program to get an on-chain "cannot mint past reserves" guarantee.
- **A reference for enforcing an economic invariant on-chain rather than off-chain.** The pattern of reading `Mint.supply` live, owning mint authority with a PDA, and re-reading state after a CPI to prove it moved is reusable well beyond stablecoins.
- **Open-source and tested from day one.** The invariant is unit-tested and the instructions are tested against the real token program, so what we hand the ecosystem is verifiable, not just described.

## 6. Sustainability and honest limitations

Stated plainly, because a grant reviewer deserves it and because it is true:

- **No users.** No one is using this today. There is no adoption to report.
- **No mainnet.** This is devnet and testnet only, by design and for the duration of this grant. No real funds are ever at risk in the funded work.
- **No revenue and no partnerships.** We are not claiming any commercial traction, integrations, or issuer relationships. If any arise later they would be a separate, disclosed matter.
- **Not yet deployed at the time of writing.** As Milestone 1 makes explicit, the deployment is the first funded step, not a thing we are pretending is already done.

Sustainability after the grant rests on the artifact being a genuine public good: an open-source, tested reserve gate plus an SDK and docs that let issuers adopt it without us in the loop. Because the core is a small, framework-independent invariant with one source of truth, it is cheap to maintain and easy for others to audit and extend. We would rather ship a narrow thing that provably works and is reusable than a broad thing that needs us to keep it alive.

---

**Citation**

```bibtex
@software{sarkar_paylane_2026,
  author  = {Dipankar Sarkar},
  title   = {PayLane: A Reserve-Gated Stablecoin Treasury on Solana},
  year    = {2026},
  url     = {https://github.com/doom2quake/paylane}
}
```

License: MIT. Testnet and devnet only; no mainnet and no real funds.
