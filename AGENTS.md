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

After any Codex context compaction, handoff, shell restart, or worktree switch, recover persisted experiment state before making a code or payload change:

```sh
./tools/amethystctl status
./tools/amethystctl payload inspect --name mithril
```

Read the last run manifest/host events named by `status` when a previous run exists. Do not reconstruct device/candidate state from conversational memory.

Read these before changing agent behavior:

- `WORKFLOW.md` — the autonomous development loop and retry policy.
- `docs/agent/architecture.md` — ownership and identity boundaries.
- `docs/agent/failure-taxonomy.md` — stable failure classes and recovery.
- `docs/agent/physical-device-gates.md` — physical-iPad-only claims.
- `agent-protocol/v2.schema.json` and `agent-protocol/lab-event-v1.schema.json` — machine contracts.

## Invariants

1. `amethystd` owns long-lived device/JIT/run state. Routine agent work uses `./tools/amethystctl`; do not recreate raw PID/port/container/sleep orchestration in prompts.
2. Command acceptance is not observed success. Every meaningful transition requires positive evidence.
3. Every Minecraft smoke/A-B run starts a fresh Amethyst native process. A new `game_session_generation` is not permission to call `JLI_Launch` a second time inside an existing Amethyst/JVM lifetime.
4. Host `process_generation` and Minecraft `game_session_generation` are separate identities. A change/end of either invalidates session-scoped JIT, renderer, menu, world and benchmark proof.
5. The device active payload pointer/manifest is authoritative. A host candidate cache never overrides what is actually active on the iPad.
6. The exact device-confirmed candidate digest is the experiment identity. A cleaned/reconstructed renderer branch cannot replace a last-known-good visual baseline until that exact new candidate independently reproduces the previous gate under the same harness/profile/instance.
7. Native `renderer_bridge_ready` proves only Amethyst's bridge handle. Semantic `renderer_ready` for Minecraft 26.2 comes from the Fabric probe and must prove the actual LWJGL `GL.getFunctionProvider()` plus representative GL function addresses against the requested staged image/digest.
8. A hot renderer must be selected before `JLI_Launch`: `org.lwjgl.opengl.libname` receives the verified absolute staged path. A later `System.setProperty` is not accepted as consumer-identity proof.
9. Persistent configuration never contains an iOS container UUID. Use Documents-relative paths/logical payload names.
10. Device transfers are staged and SHA-verified before activation. `payload clear` removes only the active pointer and preserves immutable staging content.
11. UniversalJIT26 is fail-closed. Debugger attachment alone is not `JIT_RX_MAPPING_OK`.
12. F3, screenshots and `game_running` are diagnostics, never semantic menu/world gates. Minecraft 26.2 semantics come from the test-only Fabric probe.
13. Read-only House Arrest/AFC operations may retry a bounded set of transient transport failures by reopening the service. State-changing writes are never transparently replayed because their commit point can be ambiguous.
14. Device/transport/JIT infrastructure failures remain run evidence but do not consume the renderer candidate's unchanged-failure retry budget.
15. Repeating the same semantic candidate/profile/target failure without changed evidence is prohibited after two identical fingerprints. An exceptional repeat needs `--allow-repeat-failure --retry-reason '<why>'` so it is auditable.
16. After a hypothesis is accepted, remove temporary diagnostics, run regression/CI checks, commit and push it before starting the next hypothesis. Do not accumulate a multi-cause dirty renderer tree.
17. A physical-device claim cites a fresh run artifact. CI evidence proves host/native/protocol contracts, not iPad rendering correctness or performance.

## Validation

For host-harness changes:

```sh
python3 -m compileall -q tools/amethystd tools/amethystctl.py
python3 -m unittest discover -s tests/agent -v
python3 -m json.tool agent-protocol/v2.schema.json >/dev/null
python3 -m json.tool agent-protocol/lab-event-v1.schema.json >/dev/null
```

For native control/provenance changes, require the macOS 26 native-build Actions job. For MC26.2 semantic changes, require the Java 25 Fabric probe build. Only a physical run can satisfy the gates in `docs/agent/physical-device-gates.md`.
