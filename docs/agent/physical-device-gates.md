# Physical-device gates

CI and repository tests can prove state-machine, protocol, JSON, retry, manifest, RSP framing, and packaging invariants. They cannot prove iPad-only behavior.

## One-time bootstrap prerequisites

- trusted USB pairing;
- Developer Mode enabled where required;
- valid Apple Development signing/provisioning for the AgentDebug bundle;
- a preconfigured test account/profile or other noninteractive way to reach the desired Minecraft test scenario;
- the bundled `tools/amethystd/universal_jit26_processor.py` (or an explicitly configured compatible override); no pair records, credentials, or signing material are committed.

## Gate A — device/JIT smoke

From a clean host supervisor state:

1. discover the exact target device and AgentDebug bundle;
2. launch/reconcile Amethyst and observe Agent v2 `process_generation`;
3. reconcile the iPad active renderer pointer/manifest, then create a fresh `game_session_generation`;
4. establish userspace debugserver forwarding keyed to host + game-session identity;
5. attach the UniversalJIT26 processor and observe `AMETHYST_JIT_PROCESSOR_ATTACHED`;
6. request Minecraft launch with that session generation so `launchJVM` can emit the UniversalJIT26 breakpoints;
7. observe host-side UniversalJIT26 RX allocation/page preparation;
8. independently observe fresh Amethyst log proof for `Got JIT mapping` and the RW/RX mapping;
9. when external dylibs are required, keep the processor attached and independently observe both DyldLVBypass hook successes;
10. for a hot renderer, require runtime symbol provenance to the device-confirmed staged digest/path;
11. re-check host and game-session identity; any mismatch invalidates the proof;
12. terminate/return to launcher, observe that session ended, and collect the run artifact bundle.

Debugger attachment alone is not a PASS. Any PID/process-generation change or new Minecraft game session forces JIT re-establishment.

## Gate B — Minecraft smoke

`./tools/amethystctl run smoke --profile directmetal-26.2 --target WORLD_READY`

Acceptance requires a single run/session artifact proving each reached stage. No manual taps, F3 interpretation, screenshots, panorama motion, `game_running`, or fixed sleep may be used as semantic readiness evidence. For Minecraft 26.2 the repository-built Fabric probe owns TitleScreen/JOIN/player-camera/chunk-stability semantics. If that adapter is not installed/wired, `WORLD_READY` remains unproven.

## Gate C — graphics correctness

Use deterministic camera/world checkpoints and orientation-normalized screenshots or renderer readbacks. UI automation is permitted only as a fallback to reproduce an input/UI issue; it is not the benchmark control plane.

## Gate D — performance

After correctness passes:

- fixed device settings, resolution/FOV/render distance/world/path;
- chunk stabilization;
- warmup period;
- repeated measurement runs;
- p50/p95/p99 frame time and hitch counts;
- device CPU/memory/thermal data when available;
- optional Instruments/Metal trace for deeper GPU analysis.

A candidate is not accepted as a performance optimization from CI/microbenchmark evidence alone when the claim is specifically about real iPad Minecraft performance.

## Evidence bundle

Each physical run writes only local, non-versioned artifacts under `.amethyst-agent/artifacts/<run_id>/`. Device identifiers, pairing material, account tokens, crash contents, screenshots, and Instruments traces must not be committed by default.
