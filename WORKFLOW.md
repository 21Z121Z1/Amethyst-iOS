# Unattended physical-iPad workflow

## Goal

Make real-device Amethyst/Minecraft debugging a resumable, evidence-driven transaction. Codex chooses hypotheses and code changes; deterministic software owns Python/runtime bootstrap, device connections, host/game-session identity, JIT lifetime, payload provenance, transfers, telemetry and artifact collection.

## One-time host bootstrap

```sh
./tools/bootstrap-agent
./tools/amethystctl configure --device <UDID> --bundle-id <AgentDebug bundle id>
./tools/amethystctl doctor
```

When `pymobiledevice3 usbmux list` reports the target as `Network` rather than
`USB`, export `AMETHYST_DEVICE_CONNECTION_TYPE=Network` before starting or
restarting the daemon. The default remains `USB`, and unsupported values fail
closed.

Automatic crash collection uses a bounded app-focused basename match by
default. Set `AMETHYST_CRASH_MATCH` only when a diagnostic run needs a wider
device crash set.

Use `./tools/amethystctl` thereafter. If a stale daemon belongs to another Python interpreter:

```sh
./tools/amethystctl daemon restart
```

Do not work around daemon/socket/Python errors with ad-hoc background processes. `doctor` reports the selected Python and short AF_UNIX runtime socket contract.

## Resume before changing anything

After context compaction, agent handoff, shell restart, or worktree switch, recover the persisted transaction before staging or editing a candidate:

```sh
./tools/amethystctl status
./tools/amethystctl payload inspect --name mithril
```

If `status` names a prior run, read its `.amethyst-agent/artifacts/<run-id>/manifest.json` and `host-events.jsonl`. The device-confirmed payload digest and last observed process/session identities outrank conversational memory or branch names.

## Candidate and golden-baseline workflow

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

When a physical run establishes a last-known-good visual/semantic baseline, preserve its exact candidate digest, source commit/worktree and run ID. A cleaned or reconstructed branch is a new candidate even if it contains a subset of fixes from that baseline. It may replace the golden baseline only after it independently reproduces the previous gate with the same Amethyst harness build, profile, Minecraft instance and target. Until then, A/B means restoring the exact recorded candidates; branch ancestry or intent is not evidence of equivalence.

## Required run loop

1. Bootstrap/reuse the supported harness environment and healthy `amethystd`.
2. Reconcile persisted state (`status`) and the device active candidate (`payload inspect`) before changing anything.
3. `doctor` resolves the exact target device/bundle and transport prerequisites.
4. Create a unique run ID; evidence never crosses runs.
5. Launch Amethyst with CoreDevice `--terminate-existing`. Every Minecraft smoke/A-B run receives a fresh native process/JLI/JVM lifetime; a second `JLI_Launch` in an existing Amethyst process is not a supported harness path.
6. Observe the new `process_generation` and read the iPad's active candidate pointer/manifest; reconcile candidate digest before profile/JIT work.
7. Apply deterministic profile/runtime configuration through Agent v2.
8. Generate a new `game_session_generation` for this Minecraft launch and prepare the matching Fabric lab run.
9. Attach a fresh identity-matching UniversalJIT26 processor keyed by host process + game session; request Minecraft launch carrying the same session generation.
10. Require host RX preparation plus fresh app RW/RX mapping evidence. If unsigned dynamic dylib loading is required, also require keep-attached and both current-session DyldLVBypass proofs.
11. For hot Mithril, resolve and verify the active content-addressed file before `JLI_Launch`, then pass its absolute path in `-Dorg.lwjgl.opengl.libname=...`. Do not rely on a later `System.setProperty` to choose the Minecraft GL consumer.
12. Treat native `renderer_bridge_ready` as diagnostic bridge proof only. Advance semantic `RENDERER_READY` only from the current-session MC26.2 Fabric probe after it observes the actual LWJGL `GL.getFunctionProvider()`, representative GL function addresses, and a staged path/digest match.
13. Advance Minecraft stages only from current-session app/lab events. The MC26.2 Fabric probe, not screenshots/F3, owns `menu_ready`, `world_ready`, and `chunks_stable` semantics.
14. On failure, auto-collect state, bounded logs, lab/app events, screenshot and crash evidence. Classify the first failing invariant and modify only that layer.
15. Read-only AFC operations may reopen House Arrest and retry a bounded transient transport error. State-changing writes are not transparently replayed because an interrupted operation can have an ambiguous commit point; reconcile before retrying it.
16. Re-run with a new run ID. Device/transport/JIT infrastructure failures remain evidence but do not consume the renderer candidate's unchanged-failure budget. After two identical **semantic candidate** failure fingerprints for the same device-confirmed candidate/profile/target, unchanged retries are blocked.
17. An intentional third diagnostic repeat must name why it adds evidence:

```sh
./tools/amethystctl run smoke --profile directmetal-26.2 --target MENU_READY \
  --allow-repeat-failure --retry-reason 'capture Metal validation after enabling diagnostic X'
```

18. Once a fix passes its correctness gate, remove one-shot diagnostics, run local/CI regressions, commit/push that accepted change, then choose the next first failing layer.
19. Performance A/B work starts only after semantic and graphics correctness gates pass.

## Reconciliation and invalidation

- device disconnect/reconnect: invalidate service handles and all process/JIT state;
- every smoke/A-B launch: terminate the previous Amethyst host and require a newly observed `process_generation` before treating the run as valid;
- Amethyst PID/`process_generation` change: invalidate all process/session proof;
- new Minecraft launch: create a new `game_session_generation` and invalidate previous JIT/Dyld/JVM/renderer/menu/world/chunk proof;
- observed return to launcher: end the current game session and invalidate session-scoped proof, but do not use that host for another smoke JVM launch;
- iPad active payload digest change: recompute the attempt identity and retry history;
- JIT processor/debugserver exit: invalidate JIT/Dyld readiness;
- install timeout: `UNKNOWN` until installed metadata is reconciled.

## Retry policy

Read-only transport failures may use bounded backoff while reopening House Arrest/AFC. Known stale debugserver/E96 state is torn down and recreated under the current host/game session. Device/transport/JIT orchestration failures are retained in run artifacts but are excluded from the semantic candidate two-strike guard. The retry guard keys the experiment by device-confirmed candidate digest, profile, target and dynamic-dylib requirement, then fingerprints semantic failure text while normalizing volatile PID/port/address values.

Never change renderer code to compensate for a device, signing, transfer, Python, socket or JIT infrastructure failure.

## User-intervention blockers

Report a user blocker only for conditions the host cannot safely resolve unattended, including persistent physical disconnect, device unlock/trust/Developer Mode confirmation, missing/expired signing material, account authentication/2FA, or operations with ambiguous user-data risk. Minecraft crashes, E96/stale debugserver, process restarts, log collection, renderer failures and install timeouts are diagnostic states, not automatic user blockers.
