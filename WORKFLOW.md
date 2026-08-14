# Unattended physical-iPad workflow

## Goal

Make real-device Amethyst/Minecraft debugging a resumable, evidence-driven transaction. Codex chooses hypotheses and code changes; deterministic software owns Python/runtime bootstrap, device connections, host/game-session identity, JIT lifetime, payload provenance, transfers, telemetry and artifact collection.

## One-time host bootstrap

```sh
./tools/bootstrap-agent
./tools/amethystctl configure --device <UDID> --bundle-id <AgentDebug bundle id>
./tools/amethystctl doctor
```

Use `./tools/amethystctl` thereafter. If a stale daemon belongs to another Python interpreter:

```sh
./tools/amethystctl daemon restart
```

Do not work around daemon/socket/Python errors with ad-hoc background processes. `doctor` reports the selected Python and short AF_UNIX runtime socket contract.

## Candidate workflow

Inspect what the iPad actually has active before changing renderer code:

```sh
./tools/amethystctl payload inspect --name mithril
```

Stage a new candidate:

```sh
./tools/amethystctl payload stage /path/to/libmithril.dylib --name mithril
./tools/amethystctl payload inspect --name mithril
```

Return to bundled renderer without deleting immutable staged evidence:

```sh
./tools/amethystctl payload clear --name mithril
```

A staged candidate is not considered active merely because the Mac cached its digest. `run smoke` reconciles the iPad active pointer/manifest first and records that device-confirmed digest in the run.

## Required run loop

1. Bootstrap/reuse the supported harness environment and healthy `amethystd`.
2. `doctor` resolves the exact target device/bundle and transport prerequisites.
3. Create a unique run ID; evidence never crosses runs.
4. Launch/reconcile the Amethyst host and observe `process_generation`.
5. Read the iPad's active candidate pointer/manifest; reconcile candidate digest before profile/JIT work.
6. Apply deterministic profile/runtime configuration through Agent v2.
7. Generate a new `game_session_generation` for this Minecraft launch. It changes on every launch even if the Amethyst PID is unchanged.
8. Attach a fresh or identity-matching UniversalJIT26 processor keyed by host process + game session; request Minecraft launch carrying the same session generation.
9. Require host RX preparation plus fresh app RW/RX mapping evidence. If unsigned dynamic dylib loading is required, also require keep-attached and both current-session DyldLVBypass proofs.
10. Require hot-payload runtime provenance: representative EGL/GL symbols must resolve to the exact expected staged image/digest before `renderer_ready` can be accepted.
11. Advance Minecraft stages only from current-session app/lab events. The MC26.2 Fabric probe, not screenshots/F3, owns `menu_ready`, `world_ready`, and `chunks_stable` semantics.
12. On failure, auto-collect state, bounded logs, lab/app events, screenshot and crash evidence. Classify the first failing invariant and modify only that layer.
13. Re-run with a new run ID. After two identical failure fingerprints for the same device-confirmed candidate/profile/target, unchanged retries are blocked.
14. An intentional third diagnostic repeat must name why it adds evidence:

```sh
./tools/amethystctl run smoke --profile directmetal-26.2 --target MENU_READY \
  --allow-repeat-failure --retry-reason 'capture Metal validation after enabling diagnostic X'
```

15. Once a fix passes its correctness gate, remove one-shot diagnostics, run local/CI regressions, commit/push that accepted change, then choose the next first failing layer.
16. Performance A/B work starts only after semantic and graphics correctness gates pass.

## Reconciliation and invalidation

- device disconnect/reconnect: invalidate service handles and all process/JIT state;
- Amethyst PID/`process_generation` change: invalidate all process/session proof;
- new Minecraft launch: create a new `game_session_generation` and invalidate previous JIT/Dyld/JVM/renderer/menu/world/chunk proof even when PID is unchanged;
- observed return to launcher: end the current game session and invalidate session-scoped proof;
- iPad active payload digest change: recompute the attempt identity and retry history;
- JIT processor/debugserver exit: invalidate JIT/Dyld readiness;
- install timeout: `UNKNOWN` until installed metadata is reconciled.

## Retry policy

Transport failures may use bounded backoff. Known stale debugserver/E96 state is torn down and recreated under the current game session. The retry guard keys the experiment by device-confirmed candidate digest, profile, target and dynamic-dylib requirement, then fingerprints semantic failure text while normalizing volatile PID/port/address values.

Never change renderer code to compensate for a device, signing, transfer, Python, socket or JIT infrastructure failure.

## User-intervention blockers

Report a user blocker only for conditions the host cannot safely resolve unattended, including persistent physical disconnect, device unlock/trust/Developer Mode confirmation, missing/expired signing material, account authentication/2FA, or operations with ambiguous user-data risk. Minecraft crashes, E96/stale debugserver, process restarts, log collection, renderer failures and install timeouts are diagnostic states, not automatic user blockers.
