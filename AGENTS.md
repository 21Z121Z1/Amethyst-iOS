# Amethyst agent development map

This repository supports unattended physical-device development through the host harness in `tools/amethystd` and the app-side control protocol in `Natives/AgentControl.*`.

## Stable Codex entrypoint

Do not depend on whatever Python happens to be active in the Codex shell. Bootstrap once, then use the wrapper for every harness command:

```sh
./tools/bootstrap-agent
./tools/amethystctl configure --device <UDID> --bundle-id <AgentDebug bundle id>
./tools/amethystctl doctor
```

The bootstrap creates the repository-local harness venv while the daemon socket is deliberately placed in a short hashed runtime directory under `/tmp`. `amethystctl` rejects/restarts a daemon running under a different Python interpreter instead of silently mixing environments.

Read these before changing agent behavior:

- `WORKFLOW.md` — autonomous development loop and retry policy.
- `docs/agent/architecture.md` — ownership and identity boundaries.
- `docs/agent/failure-taxonomy.md` — stable failure classes and recovery.
- `docs/agent/physical-device-gates.md` — physical-iPad-only claims.
- `agent-protocol/v2.schema.json` and `agent-protocol/lab-event-v1.schema.json` — machine contracts.

## Invariants

1. `amethystd` owns long-lived device/JIT/run state. Routine agent work uses `./tools/amethystctl`; do not recreate raw PID/port/container/sleep orchestration in prompts.
2. Command acceptance is not observed success. Every meaningful transition requires positive evidence.
3. Host `process_generation` and Minecraft `game_session_generation` are separate identities. A change/end of either invalidates session-scoped JIT, renderer, menu, world and benchmark proof.
4. The device active payload pointer/manifest is authoritative. A host candidate cache never overrides what is actually active on the iPad.
5. A hot renderer is `renderer_ready` only when runtime provenance proves representative EGL/GL symbols resolve to the exact verified staged Mach-O image. File transfer/dlopen alone is insufficient.
6. Persistent configuration never contains an iOS container UUID. Use Documents-relative paths/logical payload names.
7. Device transfers are staged and SHA-verified before activation. `payload clear` removes only the active pointer and preserves immutable staging content.
8. UniversalJIT26 is fail-closed. Debugger attachment alone is not `JIT_RX_MAPPING_OK`.
9. F3, screenshots and `game_running` are diagnostics, never semantic menu/world gates. Minecraft 26.2 semantics come from the test-only Fabric probe.
10. Repeating the same candidate/profile/target failure without changed evidence is prohibited after two identical fingerprints. An exceptional repeat needs `--allow-repeat-failure --retry-reason '<why>'` so it is auditable.
11. After a hypothesis is accepted, remove temporary diagnostics, run regression/CI checks, commit and push it before starting the next hypothesis. Do not accumulate a multi-cause dirty renderer tree.
12. A physical-device claim cites a fresh run artifact. CI evidence proves host/native/protocol contracts, not iPad rendering correctness or performance.

## Validation

For host-harness changes:

```sh
python3 -m compileall -q tools/amethystd tools/amethystctl.py
python3 -m unittest discover -s tests/agent -v
python3 -m json.tool agent-protocol/v2.schema.json >/dev/null
python3 -m json.tool agent-protocol/lab-event-v1.schema.json >/dev/null
```

For native control/provenance changes, require the macOS 26 native-build Actions job. For MC26.2 semantic changes, require the Java 25 Fabric probe build. Only a physical run can satisfy the gates in `docs/agent/physical-device-gates.md`.
