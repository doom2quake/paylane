//! reserve-core — a thin, toolchain-free harness around the PayLane reserve
//! invariant so `cargo test` runs the security-critical math without the Solana
//! SBF toolchain. It pulls in the *exact same* `reserve.rs` the on-chain Anchor
//! program uses (`programs/paylane/src/reserve.rs`) via `include!`, so there is
//! one source of truth: the code these tests prove correct is the code that runs
//! on-chain.

include!("../../programs/paylane/src/reserve.rs");
