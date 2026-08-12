# Amethyst agent development map

This repository supports unattended physical-device development through the host harness in `tools/amethystd` and the app-side control protocol in `Natives/AgentControl.*`.

## Source of truth

Read these before changing agent behavior:

- `WORKFLOW.md` — the autonomous development loop and stop conditions.
- `docs/agent/architecture.md` — ownership boundaries between Codex, the host supervisor, pymobiledevice3/CoreDevice, Amethyst, and Minecraft.
- `docs/agent/failure-taxonomy.md` — stable failure classes and recovery policy.
- `docs/agent/physical-device-gates.md` — what can only be proven on a trusted physical iPad.
- `agent-protocol/v2.schema.json` — machine-readable app control contract.

## Invariants

1. `amethystd` is the authoritative owner of long-lived device/JIT/run state. Routine agent work must use `amethystctl`; do not build ad-hoc PID/port/container-UUID state in prompts or shell scripts.
2. Command acceptance is not observed success. Every state transition that matters must have an observable proof.
3. A process-generation change invalidates JIT, Dyld-bypass, JVM, and Minecraft readiness.
4. Persistent configuration must never contain an iOS container UUID. Use paths relative to `POJAV_HOME`/Documents or logical payload identifiers.
5. Device transfers are staged and verified before activation. A command exit code is not proof that the resulting device state is correct.
6. UniversalJIT26 is fail-closed. Debugger attachment alone is not `JIT_RX_MAPPING_OK`.
7. AltStore is not part of the unattended development lane. Development signing/device deployment is a separate bootstrap concern.
8. Do not expose pairing records, credentials, account tokens, crash artifacts, device identifiers, or local signing material in Git.
9. UI automation is a fallback for reproducing UI/input bugs, not the primary Minecraft launch or benchmark mechanism.
10. A physical-device claim must cite a fresh run artifact. CI-only evidence cannot prove JIT, renderer, graphics correctness, or device performance.

## Validation

For host-harness changes run:

```sh
python3 -m unittest discover -s tests/agent -v
python3 -m compileall -q tools/amethystd tools/amethystctl.py
```

For native control-protocol changes also build the native target/IPA on the supported macOS/Xcode environment and run the physical-device smoke gate described in `docs/agent/physical-device-gates.md`.
