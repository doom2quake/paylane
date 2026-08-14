//! End-to-end tests for the PayLane program against the REAL SPL Token program.
//!
//! `solana-program-test` boots an in-process bank whose genesis already contains
//! the real `spl_token` SBF binary. Every `mint_to` / `burn` / `set_authority`
//! below is therefore a genuine cross-program invocation into that binary, and
//! every supply figure asserted here is read back out of the actual `Mint`
//! account. A stubbed-out mint helper cannot pass a single one of these tests:
//! they all assert on `Mint.supply` and on token-account balances, not on
//! anything PayLane wrote down about itself.
//!
//! What this does NOT prove: that the program has been deployed to devnet, or
//! that it compiles to SBF. `cargo build-sbf` / `anchor build` need the Solana
//! toolchain, which is not installed here. See "Toolchain honesty" in README.md.

use anchor_lang::{AccountDeserialize, InstructionData, ToAccountMetas};
use paylane::PaylaneError;
use solana_program_test::{processor, BanksClient, ProgramTest};
use solana_sdk::{
    account_info::AccountInfo,
    entrypoint::ProgramResult,
    hash::Hash,
    instruction::{Instruction, InstructionError},
    program_pack::Pack,
    pubkey::Pubkey,
    signature::{Keypair, Signer},
    system_instruction,
    transaction::{Transaction, TransactionError},
};

/// Bridge Anchor's generated `entry` (which wants `&'info [AccountInfo<'info>]`)
/// to the runtime's `ProcessInstruction` shape. The clone is leaked because the
/// runtime reclaims its own buffer when the instruction returns; test processes
/// are short-lived so this is bounded and deliberate.
fn paylane_entry<'info>(
    program_id: &Pubkey,
    accounts: &[AccountInfo<'info>],
    data: &[u8],
) -> ProgramResult {
    let leaked: &'info mut [AccountInfo<'info>] = Box::leak(accounts.to_vec().into_boxed_slice());
    paylane::entry(program_id, leaked, data)
}

const DECIMALS: u8 = 6;

struct Env {
    banks: BanksClient,
    payer: Keypair,
    blockhash: Hash,
    /// Mint authority before `initialize`, treasury authority after it.
    issuer: Keypair,
    attestor: Keypair,
    mint: Pubkey,
    treasury: Pubkey,
}

impl Env {
    async fn send(&mut self, ixs: &[Instruction], signers: Vec<&Keypair>) -> Result<(), u32> {
        let mut all: Vec<&Keypair> = vec![&self.payer];
        all.extend(signers);
        let tx = Transaction::new_signed_with_payer(
            ixs,
            Some(&self.payer.pubkey()),
            &all,
            self.blockhash,
        );
        match self.banks.process_transaction(tx).await {
            Ok(()) => Ok(()),
            Err(e) => Err(custom_code(&e)),
        }
    }

    async fn supply(&mut self) -> u64 {
        let acct = self.banks.get_account(self.mint).await.unwrap().unwrap();
        spl_token::state::Mint::unpack(&acct.data).unwrap().supply
    }

    async fn mint_authority(&mut self) -> Option<Pubkey> {
        let acct = self.banks.get_account(self.mint).await.unwrap().unwrap();
        spl_token::state::Mint::unpack(&acct.data)
            .unwrap()
            .mint_authority
            .into()
    }

    async fn balance(&mut self, token_account: Pubkey) -> u64 {
        let acct = self.banks.get_account(token_account).await.unwrap().unwrap();
        spl_token::state::Account::unpack(&acct.data).unwrap().amount
    }

    async fn treasury_state(&mut self) -> paylane::Treasury {
        let acct = self.banks.get_account(self.treasury).await.unwrap().unwrap();
        paylane::Treasury::try_deserialize(&mut acct.data.as_slice()).unwrap()
    }

    /// Create and initialize a fresh SPL token account owned by `owner`.
    async fn new_token_account(&mut self, owner: &Pubkey) -> Pubkey {
        let acct = Keypair::new();
        let rent = self.banks.get_rent().await.unwrap();
        let ixs = [
            system_instruction::create_account(
                &self.payer.pubkey(),
                &acct.pubkey(),
                rent.minimum_balance(spl_token::state::Account::LEN),
                spl_token::state::Account::LEN as u64,
                &spl_token::id(),
            ),
            spl_token::instruction::initialize_account(
                &spl_token::id(),
                &acct.pubkey(),
                &self.mint,
                owner,
            )
            .unwrap(),
        ];
        self.send(&ixs, vec![&acct]).await.expect("token account");
        acct.pubkey()
    }

    fn attest_ix(&self, new_reserve: u64) -> Instruction {
        self.attest_ix_signed_by(&self.attestor.pubkey(), new_reserve)
    }

    fn attest_ix_signed_by(&self, attestor: &Pubkey, new_reserve: u64) -> Instruction {
        Instruction {
            program_id: paylane::ID,
            accounts: paylane::accounts::Attest {
                treasury: self.treasury,
                attestor: *attestor,
                mint: self.mint,
            }
            .to_account_metas(None),
            data: paylane::instruction::AttestReserve { new_reserve }.data(),
        }
    }

    fn mint_ix(&self, recipient_token_account: Pubkey, amount: u64) -> Instruction {
        self.mint_ix_signed_by(&self.issuer.pubkey(), recipient_token_account, amount)
    }

    fn mint_ix_signed_by(
        &self,
        authority: &Pubkey,
        recipient_token_account: Pubkey,
        amount: u64,
    ) -> Instruction {
        Instruction {
            program_id: paylane::ID,
            accounts: paylane::accounts::MintTokens {
                treasury: self.treasury,
                authority: *authority,
                mint: self.mint,
                recipient_token_account,
                token_program: spl_token::id(),
            }
            .to_account_metas(None),
            data: paylane::instruction::Mint { amount }.data(),
        }
    }

    fn settle_ix(&self, holder: &Pubkey, holder_token_account: Pubkey, amount: u64) -> Instruction {
        Instruction {
            program_id: paylane::ID,
            accounts: paylane::accounts::Settle {
                treasury: self.treasury,
                mint: self.mint,
                holder_token_account,
                holder: *holder,
                token_program: spl_token::id(),
            }
            .to_account_metas(None),
            data: paylane::instruction::Settle { amount }.data(),
        }
    }
}

fn custom_code(err: &solana_program_test::BanksClientError) -> u32 {
    match err {
        solana_program_test::BanksClientError::TransactionError(
            TransactionError::InstructionError(_, InstructionError::Custom(code)),
        ) => *code,
        other => panic!("expected a program error, got {other:?}"),
    }
}

fn code(e: PaylaneError) -> u32 {
    u32::from(e)
}

/// Boot a bank, create a real SPL mint controlled by `issuer`, and (unless
/// `init == false`) run `initialize` so the treasury PDA owns the mint.
async fn boot(init: bool) -> Env {
    let pt = ProgramTest::new("paylane", paylane::ID, processor!(paylane_entry));
    let (banks, payer, blockhash) = pt.start().await;

    let issuer = Keypair::new();
    let attestor = Keypair::new();
    let mint_kp = Keypair::new();
    let mint = mint_kp.pubkey();
    let (treasury, _) =
        Pubkey::find_program_address(&[paylane::TREASURY_SEED, mint.as_ref()], &paylane::ID);

    let mut env = Env {
        banks,
        payer,
        blockhash,
        issuer,
        attestor,
        mint,
        treasury,
    };

    let rent = env.banks.get_rent().await.unwrap();
    let create = [
        system_instruction::create_account(
            &env.payer.pubkey(),
            &mint,
            rent.minimum_balance(spl_token::state::Mint::LEN),
            spl_token::state::Mint::LEN as u64,
            &spl_token::id(),
        ),
        spl_token::instruction::initialize_mint(
            &spl_token::id(),
            &mint,
            &env.issuer.pubkey(),
            None,
            DECIMALS,
        )
        .unwrap(),
    ];
    env.send(&create, vec![&mint_kp]).await.expect("create mint");

    if init {
        let issuer = env.issuer.insecure_clone();
        let ix = initialize_ix(&env, &issuer.pubkey(), env.attestor.pubkey());
        env.send(&[ix], vec![&issuer]).await.expect("initialize");
    }
    env
}

fn initialize_ix(env: &Env, mint_authority: &Pubkey, attestor: Pubkey) -> Instruction {
    Instruction {
        program_id: paylane::ID,
        accounts: paylane::accounts::Initialize {
            treasury: env.treasury,
            mint: env.mint,
            mint_authority: *mint_authority,
            payer: env.payer.pubkey(),
            token_program: spl_token::id(),
            system_program: solana_sdk::system_program::id(),
        }
        .to_account_metas(None),
        data: paylane::instruction::Initialize { attestor }.data(),
    }
}

// --- initialize -------------------------------------------------------------

/// Finding 5: `initialize` used to be permissionless on a global PDA, so the
/// first caller could seize the treasury. Now the PDA is per-mint and only the
/// current mint authority can create it, and the mint authority moves to the
/// PDA in the same transaction.
#[tokio::test]
async fn initialize_binds_the_mint_and_hands_authority_to_the_pda() {
    let mut env = boot(true).await;
    assert_eq!(
        env.mint_authority().await,
        Some(env.treasury),
        "after initialize the PDA, and nothing else, can mint"
    );
    let t = env.treasury_state().await;
    assert_eq!(t.mint, env.mint);
    assert_eq!(t.authority, env.issuer.pubkey());
    assert_eq!(t.attestor, env.attestor.pubkey());
    assert_eq!(t.attested_reserve, 0);
    assert!(!t.paused);
}

/// Finding 5, the attack itself: a stranger cannot front-run the treasury.
#[tokio::test]
async fn a_stranger_cannot_initialize_the_treasury_for_someone_elses_mint() {
    let mut env = boot(false).await;
    let attacker = Keypair::new();
    let ix = initialize_ix(&env, &attacker.pubkey(), attacker.pubkey());
    let err = env.send(&[ix], vec![&attacker]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::NotMintAuthority));
    // the mint is still the issuer's, and no treasury exists
    assert_eq!(env.mint_authority().await, Some(env.issuer.pubkey()));
    assert!(env.banks.get_account(env.treasury).await.unwrap().is_none());
}

/// Separated powers: the key that attests reserves must not be the key that
/// mints against them.
#[tokio::test]
async fn initialize_rejects_an_attestor_equal_to_the_mint_authority() {
    let mut env = boot(false).await;
    let issuer = env.issuer.insecure_clone();
    let ix = initialize_ix(&env, &issuer.pubkey(), issuer.pubkey());
    let err = env.send(&[ix], vec![&issuer]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::AttestorMustDiffer));
}

// --- mint -------------------------------------------------------------------

/// Finding 2: the mint CPI used to be a stub that returned `Ok(())`. This test
/// reads the real `Mint.supply` and the real token-account balance out of the
/// SPL Token program's own accounts, so a stub fails it.
#[tokio::test]
async fn mint_moves_real_spl_supply_and_credits_the_holder() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;

    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();
    env.send(&[env.attest_ix(1_000_000)], vec![&attestor])
        .await
        .unwrap();
    assert_eq!(env.supply().await, 0);

    env.send(&[env.mint_ix(ata, 400_000)], vec![&issuer])
        .await
        .unwrap();

    assert_eq!(env.supply().await, 400_000, "real SPL supply moved");
    assert_eq!(env.balance(ata).await, 400_000, "holder really holds them");
}

/// The gate, on-chain, against live `Mint.supply`.
#[tokio::test]
async fn mint_past_attested_reserves_is_rejected_and_moves_nothing() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;
    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();

    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();
    env.send(&[env.mint_ix(ata, 800)], vec![&issuer])
        .await
        .unwrap();

    let err = env
        .send(&[env.mint_ix(ata, 300)], vec![&issuer])
        .await
        .unwrap_err();
    assert_eq!(err, code(PaylaneError::ExceedsReserves));
    assert_eq!(env.supply().await, 800, "a rejected mint mints nothing");
    assert_eq!(env.balance(ata).await, 800);
}

#[tokio::test]
async fn mint_rejects_a_signer_who_is_not_the_treasury_authority() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;
    let attestor = env.attestor.insecure_clone();
    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();

    let attacker = Keypair::new();
    let ix = env.mint_ix_signed_by(&attacker.pubkey(), ata, 1_000);
    let err = env.send(&[ix], vec![&attacker]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::Unauthorized));
    assert_eq!(env.supply().await, 0);
}

/// Finding 6: mint/recipient identity used to be unchecked, so one reserve
/// ledger could be applied to an unrelated mint. The recipient account must now
/// belong to the treasury's bound mint.
#[tokio::test]
async fn mint_rejects_a_recipient_account_of_a_different_mint() {
    let mut env = boot(true).await;
    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();
    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();

    // an unrelated mint, with an unrelated token account
    let other_mint = Keypair::new();
    let rent = env.banks.get_rent().await.unwrap();
    let holder = Keypair::new();
    let other_ata = Keypair::new();
    let ixs = [
        system_instruction::create_account(
            &env.payer.pubkey(),
            &other_mint.pubkey(),
            rent.minimum_balance(spl_token::state::Mint::LEN),
            spl_token::state::Mint::LEN as u64,
            &spl_token::id(),
        ),
        spl_token::instruction::initialize_mint(
            &spl_token::id(),
            &other_mint.pubkey(),
            &holder.pubkey(),
            None,
            DECIMALS,
        )
        .unwrap(),
        system_instruction::create_account(
            &env.payer.pubkey(),
            &other_ata.pubkey(),
            rent.minimum_balance(spl_token::state::Account::LEN),
            spl_token::state::Account::LEN as u64,
            &spl_token::id(),
        ),
        spl_token::instruction::initialize_account(
            &spl_token::id(),
            &other_ata.pubkey(),
            &other_mint.pubkey(),
            &holder.pubkey(),
        )
        .unwrap(),
    ];
    env.send(&ixs, vec![&other_mint, &other_ata])
        .await
        .expect("second mint");

    let ix = env.mint_ix(other_ata.pubkey(), 500);
    let err = env.send(&[ix], vec![&issuer]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::WrongMint));
    assert_eq!(env.supply().await, 0);
}

// --- settle -----------------------------------------------------------------

/// Finding 3, the exact attack: `settle` used to lower a stored `circulating`
/// counter without touching anyone's tokens, so an issuer could settle 1,000,
/// leave the holder's 1,000 tokens in their wallet, and mint 1,000 more, ending
/// with 2,000 spendable against 1,000 of reserves.
///
/// Circulating is now `Mint.supply` and `settle` burns. This test walks the
/// attack and asserts that total spendable tokens never exceed the reserves.
#[tokio::test]
async fn settle_burns_and_cannot_manufacture_unbacked_headroom() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;
    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();

    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();
    env.send(&[env.mint_ix(ata, 1_000)], vec![&issuer])
        .await
        .unwrap();
    assert_eq!(env.supply().await, 1_000);

    // headroom is exhausted
    let err = env
        .send(&[env.mint_ix(ata, 1)], vec![&issuer])
        .await
        .unwrap_err();
    assert_eq!(err, code(PaylaneError::ExceedsReserves));

    // redeem: the holder signs and the tokens are actually destroyed
    env.send(&[env.settle_ix(&holder.pubkey(), ata, 1_000)], vec![&holder])
        .await
        .unwrap();
    assert_eq!(env.supply().await, 0, "settle burns real supply");
    assert_eq!(env.balance(ata).await, 0, "the holder's tokens are gone");

    // now the freed headroom can be reused, and the total in existence is 1,000
    let second = Keypair::new();
    let ata2 = env.new_token_account(&second.pubkey()).await;
    env.send(&[env.mint_ix(ata2, 1_000)], vec![&issuer])
        .await
        .unwrap();
    let total = env.balance(ata).await + env.balance(ata2).await;
    assert_eq!(total, 1_000, "never 2,000 spendable against 1,000 reserves");
    assert_eq!(env.supply().await, 1_000);
}

/// The holder must sign their own redemption; nobody can burn someone else's
/// tokens to make room to mint.
#[tokio::test]
async fn settle_rejects_a_signer_who_does_not_own_the_token_account() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;
    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();
    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();
    env.send(&[env.mint_ix(ata, 1_000)], vec![&issuer])
        .await
        .unwrap();

    let attacker = Keypair::new();
    let ix = env.settle_ix(&attacker.pubkey(), ata, 1_000);
    let err = env.send(&[ix], vec![&attacker]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::Unauthorized));
    assert_eq!(env.supply().await, 1_000, "nothing was burned");
}

// --- reserve loss -----------------------------------------------------------

/// Finding 4: a truthful fall in reserves used to be REJECTED, leaving a stale
/// higher figure on-chain and letting the issuer keep minting against reserves
/// that no longer existed. Now the lower figure is recorded and minting stops.
#[tokio::test]
async fn a_reserve_loss_is_recorded_on_chain_and_pauses_minting() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;
    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();

    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();
    env.send(&[env.mint_ix(ata, 800)], vec![&issuer])
        .await
        .unwrap();

    // reserves really fell to 600 against 800 circulating
    env.send(&[env.attest_ix(600)], vec![&attestor])
        .await
        .unwrap();
    let t = env.treasury_state().await;
    assert_eq!(t.attested_reserve, 600, "the truth is written down");
    assert!(t.paused, "the shortfall pauses the treasury");

    // the old bug: 1,000 stayed on the books and this 200 was allowed
    let err = env
        .send(&[env.mint_ix(ata, 200)], vec![&issuer])
        .await
        .unwrap_err();
    assert_eq!(err, code(PaylaneError::Undercollateralized));
    assert_eq!(env.supply().await, 800);
}

/// Redeeming back under the reserve line restores solvency and unpauses. The
/// follow-up mint then fails with ExceedsReserves, not Undercollateralized,
/// which proves the pause really lifted and the reserve gate is what is talking.
#[tokio::test]
async fn redeeming_back_under_the_line_unpauses_the_treasury() {
    let mut env = boot(true).await;
    let holder = Keypair::new();
    let ata = env.new_token_account(&holder.pubkey()).await;
    let attestor = env.attestor.insecure_clone();
    let issuer = env.issuer.insecure_clone();

    env.send(&[env.attest_ix(1_000)], vec![&attestor])
        .await
        .unwrap();
    env.send(&[env.mint_ix(ata, 800)], vec![&issuer])
        .await
        .unwrap();
    env.send(&[env.attest_ix(600)], vec![&attestor])
        .await
        .unwrap();
    assert!(env.treasury_state().await.paused);

    // partial redemption is not enough: 710 circulating still exceeds 600
    env.send(&[env.settle_ix(&holder.pubkey(), ata, 90)], vec![&holder])
        .await
        .unwrap();
    assert_eq!(env.supply().await, 710);
    assert!(env.treasury_state().await.paused);

    // redeem the rest of the shortfall
    env.send(&[env.settle_ix(&holder.pubkey(), ata, 110)], vec![&holder])
        .await
        .unwrap();
    let t = env.treasury_state().await;
    assert!(!t.paused, "solvency restored, treasury unpaused");
    assert_eq!(env.supply().await, 600);

    let err = env
        .send(&[env.mint_ix(ata, 1)], vec![&issuer])
        .await
        .unwrap_err();
    assert_eq!(
        err,
        code(PaylaneError::ExceedsReserves),
        "the pause lifted; the reserve ceiling is now what blocks the mint"
    );
}

#[tokio::test]
async fn attest_rejects_a_signer_who_is_not_the_attestor() {
    let mut env = boot(true).await;
    let attacker = Keypair::new();
    let ix = env.attest_ix_signed_by(&attacker.pubkey(), 9_999_999);
    let err = env.send(&[ix], vec![&attacker]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::Unauthorized));
    assert_eq!(env.treasury_state().await.attested_reserve, 0);
}

/// The issuer holds the mint key but must not be able to raise its own
/// collateral figure: separated powers, enforced on-chain.
#[tokio::test]
async fn the_minting_authority_cannot_attest_its_own_reserves() {
    let mut env = boot(true).await;
    let issuer = env.issuer.insecure_clone();
    let ix = env.attest_ix_signed_by(&issuer.pubkey(), 1_000_000);
    let err = env.send(&[ix], vec![&issuer]).await.unwrap_err();
    assert_eq!(err, code(PaylaneError::Unauthorized));
    assert_eq!(env.treasury_state().await.attested_reserve, 0);
}
