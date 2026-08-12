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
3. establish userspace debugserver forwarding for that generation;
4. attach the UniversalJIT26 processor and observe `AMETHYST_JIT_PROCESSOR_ATTACHED`;
5. request Minecraft launch so `launchJVM` can emit the UniversalJIT26 breakpoints;
6. observe host-side UniversalJIT26 RX allocation/page preparation;
7. independently observe fresh Amethyst log proof for `Got JIT mapping` and the RW/RX mapping;
8. when external dylibs are required, keep the processor attached and independently observe both DyldLVBypass hook successes;
9. re-check `process_generation`; any change invalidates all JIT evidence;
10. terminate/return to launcher and collect the run artifact bundle.

Debugger attachment alone is not a PASS. Any PID/process-generation change forces JIT re-establishment.

## Gate B — Minecraft smoke

`amethystctl run smoke --profile directmetal-26.2 --target WORLD_READY`

Acceptance requires a single run artifact proving each reached stage. No manual taps and no fixed sleep may be used as readiness evidence. If the version-specific Fabric/Minecraft lab adapter is not installed/wired, `WORLD_READY` remains unproven rather than inferred from a screenshot.

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
