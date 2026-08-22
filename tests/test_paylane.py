"""PayLane tests: the reserve invariant, treasury seam, wire format, CLI, agent.

The invariant tests mirror the pure-Rust unit tests in
`programs/paylane/src/reserve.rs` case for case, so the off-chain agent and the
on-chain program are proven to agree on every gate. The program's real on-chain
behaviour (SPL supply, burns, PDA mint authority) is proven separately by
`programs/paylane/tests/program.rs` under `solana-program-test`.
"""

import json

import pytest

from paylane.chain import (
    TREASURY_ACCOUNT_LEN,
    ChainUnavailable,
    decode_treasury,
    encode_u64,
    instruction_data,
    ix_discriminator,
)
from paylane.config import PaylaneSettings
from paylane.ledger import U64_MAX, ReserveError, ReserveState, ReserveViolation
from paylane.treasury import Treasury, TreasuryEvent
from paylane.agent import PaylaneAgent
from paylane import main as cli_main


def _cfg(**kw):
    return PaylaneSettings(offline=True, **kw)


def _treasury(**kw):
    return Treasury(cfg=_cfg(), **kw)


# --- the reserve invariant (parity with reserve.rs) --------------------------

def test_mint_within_reserves_is_allowed():
    s = ReserveState(1_000_000, 0)
    assert s.available_to_mint() == 1_000_000
    s.apply_mint(400_000)
    assert s.circulating == 400_000
    assert s.available_to_mint() == 600_000


def test_mint_exactly_to_the_ceiling_is_allowed():
    s = ReserveState(500, 100)
    s.apply_mint(400)  # 100 + 400 == 500
    assert s.circulating == 500
    assert s.available_to_mint() == 0


def test_mint_one_past_the_ceiling_is_rejected():
    s = ReserveState(500, 100)
    with pytest.raises(ReserveViolation) as ei:
        s.check_mint(401)
    assert ei.value.err is ReserveError.EXCEEDS_RESERVES


def test_mint_rejection_does_not_mutate_state():
    s = ReserveState(500, 100)
    before = (s.attested_reserve, s.circulating)
    with pytest.raises(ReserveViolation):
        s.apply_mint(9_999)
    assert (s.attested_reserve, s.circulating) == before


def test_zero_amount_mint_is_rejected():
    s = ReserveState(1_000, 0)
    with pytest.raises(ReserveViolation) as ei:
        s.check_mint(0)
    assert ei.value.err is ReserveError.ZERO_AMOUNT


def test_mint_overflow_is_caught_not_wrapped():
    s = ReserveState((1 << 64) - 1, (1 << 64) - 6)
    with pytest.raises(ReserveViolation) as ei:
        s.check_mint(100)
    assert ei.value.err is ReserveError.MATH_OVERFLOW


def test_settle_lowers_circulating_and_frees_headroom():
    s = ReserveState(1_000, 600)
    s.apply_settle(250)
    assert s.circulating == 350
    assert s.available_to_mint() == 650


def test_settle_more_than_circulating_underflows_safely():
    s = ReserveState(1_000, 100)
    with pytest.raises(ReserveViolation) as ei:
        s.check_settle(101)
    assert ei.value.err is ReserveError.MATH_OVERFLOW


def test_raising_attestation_opens_headroom():
    s = ReserveState(1_000, 1_000)
    assert s.available_to_mint() == 0
    s.apply_attest(1_500)
    assert s.available_to_mint() == 500
    s.apply_mint(500)
    with pytest.raises(ReserveViolation):
        s.check_mint(1)


def test_backing_bps_is_par_or_above_when_solvent():
    assert ReserveState(1_200, 1_000).backing_bps() == 12_000
    assert ReserveState(1_000, 1_000).backing_bps() == 10_000


def test_backing_bps_saturates_instead_of_returning_a_giant_sentinel():
    """The old sentinel was u64::MAX, which overflows an int64 column: writing
    the agent's journal to a real store raised `Value out of range`."""
    from paylane.ledger import MAX_BACKING_BPS

    assert ReserveState(1_000, 0).backing_bps() == MAX_BACKING_BPS
    assert ReserveState(U64_MAX, 1).backing_bps() == MAX_BACKING_BPS
    assert MAX_BACKING_BPS < 2**63 - 1


# --- reserve losses are recorded, not hidden (parity with reserve.rs) --------

def test_attestation_below_circulating_is_recorded_and_pauses_minting():
    """The old behaviour rejected this attestation, leaving a stale 1,000 on the
    books and letting the issuer mint another 200 against reserves that were
    gone. Matches `attestation_below_circulating_is_recorded_and_pauses_minting`
    in reserve.rs."""
    s = ReserveState(1_000, 800)
    out = s.apply_attest(600)
    assert out.solvent is False
    assert out.shortfall == 200
    assert s.attested_reserve == 600, "the real, lower reserve is recorded"
    assert s.paused is True
    assert s.available_to_mint() == 0, "a paused treasury advertises no headroom"


def test_no_minting_is_possible_while_undercollateralized():
    s = ReserveState(1_000, 800)
    s.apply_attest(600)
    for amount in (200, 1):
        with pytest.raises(ReserveViolation) as ei:
            s.check_mint(amount)
        assert ei.value.err is ReserveError.UNDERCOLLATERALIZED


def test_a_covering_attestation_unpauses_the_treasury():
    s = ReserveState(1_000, 800)
    s.apply_attest(600)
    assert s.apply_attest(900).solvent is True
    assert s.paused is False
    assert s.available_to_mint() == 100


def test_redemption_can_restore_solvency_while_paused():
    s = ReserveState(1_000, 800)
    s.apply_attest(600)
    s.apply_settle(200)  # circulating 600 == attested 600
    assert s.paused is False
    assert s.circulating == 600


def test_partial_redemption_leaves_the_treasury_paused():
    s = ReserveState(1_000, 800)
    s.apply_attest(600)
    s.apply_settle(100)  # circulating 700, still above 600
    assert s.paused is True


def test_backing_bps_reports_the_shortfall_honestly():
    s = ReserveState(1_000, 800)
    s.apply_attest(600)
    assert s.backing_bps() == 7_500


# --- wire format (what a live client would have to get right) ----------------

def test_anchor_instruction_discriminators_are_the_documented_hash():
    import hashlib

    for name in ("attest_reserve", "mint", "settle"):
        expected = hashlib.sha256(f"global:{name}".encode()).digest()[:8]
        assert ix_discriminator(name) == expected
        assert len(ix_discriminator(name)) == 8
    # distinct instructions must not collide
    assert len({ix_discriminator(n) for n in ("attest_reserve", "mint", "settle")}) == 3


def test_instruction_data_is_discriminator_plus_little_endian_u64():
    data = instruction_data("mint", 400_000)
    assert data[:8] == ix_discriminator("mint")
    assert data[8:] == (400_000).to_bytes(8, "little")
    assert len(data) == 16


def test_encode_u64_refuses_values_outside_the_u64_range():
    with pytest.raises(ValueError):
        encode_u64(-1)
    with pytest.raises(ValueError):
        encode_u64(1 << 64)


def test_unknown_instruction_names_are_refused():
    with pytest.raises(ValueError):
        instruction_data("drain_treasury", 1)


def test_treasury_account_layout_round_trips():
    """Pins the `Treasury` layout in lib.rs: 8 disc + 3 pubkeys + u64 + bool + bump."""
    assert TREASURY_ACCOUNT_LEN == 114
    raw = (
        b"\x01" * 8
        + b"\xaa" * 32
        + b"\xbb" * 32
        + b"\xcc" * 32
        + (1_234_567).to_bytes(8, "little")
        + b"\x01"
        + b"\xfd"
    )
    t = decode_treasury(raw)
    assert t.authority == b"\xaa" * 32
    assert t.attestor == b"\xbb" * 32
    assert t.mint == b"\xcc" * 32
    assert t.attested_reserve == 1_234_567
    assert t.paused is True
    assert t.bump == 253


def test_decode_treasury_refuses_a_wrong_sized_account():
    with pytest.raises(ValueError):
        decode_treasury(b"\x00" * (TREASURY_ACCOUNT_LEN - 1))


def test_decode_treasury_refuses_a_non_boolean_pause_flag():
    raw = bytearray(b"\x00" * TREASURY_ACCOUNT_LEN)
    raw[8 + 104] = 7
    with pytest.raises(ValueError):
        decode_treasury(bytes(raw))


# --- treasury seam (offline mirror of the program) ---------------------------

def test_treasury_rejects_mint_past_reserves_without_producing_a_tx():
    t = _treasury()
    t.attest_reserve(1_000)
    t.mint(800)
    ev = t.mint(300)  # 800 + 300 > 1000
    assert isinstance(ev, TreasuryEvent)
    assert ev.ok is False
    assert ev.signature == ""  # a rejected instruction produces no transaction
    assert t.state.circulating == 800  # unchanged


def test_offline_events_are_labelled_simulated_and_are_not_signatures():
    """The old mirror emitted 44-char base58 lookalikes next to a 'Solana devnet'
    badge. Offline events now say what they are."""
    t = _treasury()
    ev = t.attest_reserve(1_000)
    assert ev.simulated is True
    assert ev.signature.startswith("sim:")
    assert t.live is False


def test_treasury_records_event_history():
    t = _treasury()
    t.attest_reserve(1_000)
    ev = t.mint(400)
    assert ev.ok and ev.signature
    assert t.state.circulating == 400
    t.settle(200)
    ev2 = t.mint(800)
    assert ev2.ok
    assert t.state.circulating == 1_000
    kinds = [e.kind for e in t.history()]
    assert kinds == ["ReserveAttested", "Minted", "Settled", "Minted"]


def test_treasury_records_a_reserve_loss_and_blocks_further_minting():
    t = _treasury()
    t.attest_reserve(1_000)
    t.mint(800)
    ev = t.attest_reserve(600)
    assert ev.ok is True, "the shortfall is recorded, not refused"
    assert ev.paused is True
    assert t.state.attested_reserve == 600
    blocked = t.mint(1)
    assert blocked.ok is False
    assert "undercollateralized" in blocked.reason


def test_signatures_are_deterministic_across_runs():
    a, b = _treasury(), _treasury()
    a.attest_reserve(1_000)
    b.attest_reserve(1_000)
    assert a.mint(400).signature == b.mint(400).signature


# --- the live seam: submit first, commit second ------------------------------

class _FakeChain:
    """Stands in for a deployed program. `fail` makes submission raise the way a
    dropped RPC or a rejected transaction would."""

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []
        self.reserve = 0
        self.circulating = 0
        self.paused = False

    def submit(self, instruction, amount):
        if self.fail:
            raise RuntimeError("rpc timeout")
        self.sent.append((instruction, amount))
        if instruction == "attest_reserve":
            self.reserve = amount
            self.paused = amount < self.circulating
        elif instruction == "mint":
            self.circulating += amount
        else:
            self.circulating -= amount
            self.paused = self.paused and self.circulating > self.reserve
        return f"5{instruction[:3]}RealSigFromCluster{len(self.sent)}"

    def fetch(self):
        return (self.reserve, self.circulating, self.paused)


def test_live_mode_records_the_signature_the_cluster_returned():
    chain = _FakeChain()
    t = Treasury(cfg=_cfg(), chain=chain)
    assert t.live is True
    ev = t.attest_reserve(1_000)
    assert ev.simulated is False
    assert ev.signature.startswith("5att"), "the signature came from the cluster"
    assert not ev.signature.startswith("sim:")
    assert chain.sent == [("attest_reserve", 1_000)]


def test_a_failed_submission_leaves_the_mirror_untouched():
    """The bug this pins: the old adapter mutated state and appended a
    pseudo-signature BEFORE calling the live path, so a failure left behind a
    'successful' event for a transaction that never existed."""
    chain = _FakeChain()
    t = Treasury(cfg=_cfg(), chain=chain)
    t.attest_reserve(1_000)
    t.mint(400)
    before = (t.state.attested_reserve, t.state.circulating, len(t.history()))

    chain.fail = True
    ev = t.mint(100)
    assert ev.ok is False
    assert ev.kind == "Failed(Minted)"
    assert ev.signature == "", "no transaction, so no signature"
    assert (t.state.attested_reserve, t.state.circulating) == before[:2]
    assert len([e for e in t.history() if e.ok]) == 2, "no successful event was invented"


def test_live_mode_reloads_canonical_state_from_the_chain():
    """The chain, not the mirror, is the source of truth after a submission."""
    chain = _FakeChain()
    t = Treasury(cfg=_cfg(), chain=chain)
    t.attest_reserve(1_000)
    # 250 was minted out of band, so the mirror's idea of circulating is stale
    chain.circulating = 250
    t.mint(400)
    assert t.state.circulating == 650, "state came back from the chain, not the mirror"


def test_the_audit_journal_stays_local_unless_explicitly_opted_out(monkeypatch):
    """A stray GOOGLE_CLOUD_PROJECT in the environment used to send `paylane
    demo` at somebody's real Firestore."""
    monkeypatch.delenv("PAYLANE_IN_MEMORY_STATE", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "someone-elses-project")
    assert PaylaneSettings().use_in_memory_state is True
    monkeypatch.setenv("PAYLANE_IN_MEMORY_STATE", "0")
    assert PaylaneSettings().use_in_memory_state is False


def test_a_live_configuration_without_a_client_fails_closed():
    """Configured for devnet but with no submission client: refuse to construct
    rather than run the simulation and call it live."""
    cfg = PaylaneSettings(
        rpc_url="https://api.devnet.solana.com",
        token_mint="So11111111111111111111111111111111111111112",
        keypair_path="/tmp/does-not-exist.json",
        offline=False,
    )
    assert cfg.use_chain is True
    with pytest.raises(ChainUnavailable):
        Treasury(cfg=cfg)


# --- persistence: separate CLI invocations are one workflow ------------------

def test_treasury_snapshot_round_trips():
    t = _treasury()
    t.attest_reserve(1_000)
    t.mint(400)
    t.attest_reserve(300)  # shortfall: pauses
    restored = Treasury.restore(t.snapshot(), cfg=_cfg())
    assert restored.state.attested_reserve == 300
    assert restored.state.circulating == 400
    assert restored.state.paused is True
    assert [e.kind for e in restored.history()] == [e.kind for e in t.history()]


def test_cli_commands_share_one_treasury(tmp_path, capsys):
    """`attest`, then `mint`, then `state` used to create three unrelated
    treasuries, so no workflow was possible."""
    state = str(tmp_path / "state.json")
    assert cli_main.cli(["--state", state, "attest", "1000000000"]) == 0
    assert cli_main.cli(["--state", state, "mint", "400000000"]) == 0
    capsys.readouterr()
    assert cli_main.cli(["--state", state, "state"]) == 0
    out = capsys.readouterr().out
    assert "$1,000.00" in out and "$400.00" in out, out
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["attested_reserve"] == 1_000_000_000
    assert saved["circulating"] == 400_000_000


def test_cli_reset_clears_the_saved_treasury(tmp_path):
    state = tmp_path / "state.json"
    cli_main.cli(["--state", str(state), "attest", "1000"])
    assert state.exists()
    cli_main.cli(["--state", str(state), "reset"])
    assert not state.exists()


def test_cli_mint_past_reserves_is_refused_across_invocations(tmp_path, capsys):
    state = str(tmp_path / "state.json")
    cli_main.cli(["--state", state, "attest", "1000"])
    cli_main.cli(["--state", state, "mint", "900"])
    capsys.readouterr()
    cli_main.cli(["--state", state, "mint", "200"])
    out = capsys.readouterr().out
    assert "REJECTED" in out
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["circulating"] == 900


def test_cli_demo_runs_the_whole_story(tmp_path, capsys):
    assert cli_main.cli(["--state", str(tmp_path / "s.json"), "demo"]) == 0
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "TREASURY PAUSED" in out


# --- agent (guardrails + audit) ----------------------------------------------

def test_agent_gate_blocks_overmint_and_journals_it():
    agent = PaylaneAgent(_treasury())
    agent.attest(1_000)
    agent.mint(900)
    res = agent.mint(200)  # would exceed
    assert res["status"] == "rejected"
    assert res["event"]["ok"] is False
    assert res["circulating"] == 900
    assert res["available_to_mint"] == 100


def test_agent_end_to_end_preserves_invariant():
    agent = PaylaneAgent(_treasury())
    agent.attest(1_000_000)
    assert agent.mint(300_000)["status"] == "ok"
    assert agent.mint(500_000)["status"] == "ok"
    assert agent.mint(300_000)["status"] == "rejected"  # 800k + 300k > 1M
    assert agent.settle(100_000)["status"] == "ok"
    assert agent.mint(300_000)["status"] == "ok"  # now fits
    s = agent.state()
    assert s["circulating"] == 1_000_000
    assert s["circulating"] <= s["attested_reserve"]


def test_agent_reports_a_shortfall_and_stops_minting():
    agent = PaylaneAgent(_treasury())
    agent.attest(1_000)
    agent.mint(800)
    res = agent.attest(600)
    assert res["status"] == "ok" and res["paused"] is True
    assert agent.state()["available_to_mint"] == 0
    assert agent.mint(1)["status"] == "rejected"


def test_each_agent_has_its_own_action_budget():
    """A module-level limiter meant two independent treasuries in one process
    silently ate each other's budget, and a long test session starved later
    runs."""
    from agent_core import ActionLimiter, ActionPolicy

    policy = ActionPolicy(dry_run=False, max_actions_per_cycle=4, max_actions_per_hour=2)
    a = PaylaneAgent(_treasury(), limiter=ActionLimiter(policy))
    b = PaylaneAgent(_treasury(), limiter=ActionLimiter(policy))
    assert a.attest(1_000)["status"] == "ok"
    assert a.mint(100)["status"] == "ok"
    assert a.mint(100)["status"] == "blocked", "a's own cap still applies"
    assert b.attest(1_000)["status"] == "ok", "b has its own budget"


def test_agent_history_matches_treasury():
    agent = PaylaneAgent(_treasury())
    agent.attest(1_000)
    agent.mint(400)
    hist = agent.history()
    assert [h["kind"] for h in hist] == ["ReserveAttested", "Minted"]
    assert hist[-1]["circulating"] == 400
