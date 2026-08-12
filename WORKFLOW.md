# Unattended physical-iPad workflow

## Goal

Make real-device Amethyst/Minecraft debugging a resumable, evidence-driven transaction. Codex chooses hypotheses and code changes; deterministic software owns device connections, JIT lifetime, process identity, transfers, telemetry, and artifact collection.

## Required loop

1. Start/reuse `amethystd`.
2. `amethystctl doctor` and resolve the exact target bundle/device.
3. Start a run with a unique `run_id`; never reuse evidence from another run.
4. Launch/reconcile Amethyst and observe the app's `process_generation`.
5. Establish UniversalJIT26 for that process generation. Require observed RX/RW mapping before declaring executable JIT ready. If dynamic unsigned runtime/dylib loading is required, also require the persistent-attached/Dyld-bypass proof.
6. Submit profile/runtime requests through Agent v2 and wait for the response/event that proves the effect.
7. Launch Minecraft and advance only on observed app/Minecraft events, never fixed sleeps.
8. On failure, collect run-scoped logs/crashes/state, classify the first failing layer, then change only the responsible layer.
9. Re-run with a new `run_id`. A fix is accepted only by new evidence.
10. Once correctness gates pass, run deterministic performance A/B measurements; accept only a positive or neutral correctness result with the intended performance improvement.
11. Commit/push accepted changes and continue to the next highest-value goal.

## Reconciliation

`amethystd` treats these as invalidating events:

- device disconnect/reconnect: invalidate device service handles and all process/JIT state;
- app PID or app-provided `process_generation` change: invalidate JIT, Dyld bypass, JVM, renderer, menu, and world state;
- bundle build marker change: invalidate runtime/profile assumptions until reprobed;
- JIT processor/debugserver exit: invalidate JIT/Dyld readiness even if the app process remains alive.

An install/deploy timeout is `UNKNOWN`, not automatically `FAILED`: query the installed build marker and reconcile before retrying.

## Retry policy

Transient transport failures may be retried with bounded backoff. A repeated identical failure signature twice without new evidence must not be blindly retried; reclassify or investigate a new hypothesis.

Never change renderer code to compensate for a device, signing, transfer, or JIT infrastructure failure.

## User-intervention blockers

Stop and report a blocker only for conditions the host cannot safely resolve unattended, such as:

- physical disconnect that does not recover;
- device unlock/trust/Developer Mode confirmation;
- expired/missing provisioning or signing identity;
- account authentication/2FA interaction;
- an operation with ambiguous risk to user data;
- missing private/local UniversalJIT26 processor implementation or signing material that cannot be reconstructed from the repository.

Minecraft crashes, E96/stale debugserver state, process restarts, log collection, renderer crashes, FBO errors, and install-command timeouts are not user blockers by themselves.
