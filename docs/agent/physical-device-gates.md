# Physical-device gates

CI and repository tests can prove state-machine, protocol, JSON, retry, manifest, and packaging invariants. They cannot prove iPad-only behavior.

## One-time bootstrap prerequisites

- trusted USB pairing;
- Developer Mode enabled where required;
- valid Apple Development signing/provisioning for the AgentDebug bundle;
- a preconfigured test account/profile or other noninteractive way to reach the desired Minecraft test scenario;
- the host UniversalJIT26 breakpoint processor used by this Amethyst/iOS version, configured for `amethystd` without committing local credentials/pair records.

## Gate A — device/JIT smoke

From a clean host supervisor state:

1. discover the exact target device and AgentDebug bundle;
2. launch/reconcile Amethyst;
3. observe v2 process generation;
4. establish debugserver/processor for that generation;
5. observe UniversalJIT26 RX/RW mapping proof;
6. when external dylibs are required, keep the debugger processor attached and observe Dyld-bypass proof;
7. issue v2 status/probe requests through House Arrest;
8. terminate/return to launcher and collect run artifacts.

Any PID/process-generation change must force JIT re-establishment.

## Gate B — Minecraft smoke

`amethystctl run smoke --profile directmetal-26.2 --target WORLD_READY`

Acceptance requires a single run artifact proving each reached stage. No manual taps and no fixed sleep may be used as readiness evidence. If the Fabric lab probe is not yet installed/wired, `WORLD_READY` must remain unproven rather than inferred from a screenshot.

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
