# Agent failure taxonomy

Stable failure classes let Codex change the responsible layer rather than rediscovering infrastructure.

| Class | Meaning | Default recovery |
|---|---|---|
| `DEVICE_NOT_FOUND` | Target is not visible over the selected transport | bounded rediscovery; then blocker |
| `DEVICE_DISCONNECTED` | Previously selected device disappeared | invalidate services/process/JIT; reconnect |
| `DEVICE_LOCKED` | Required service is unavailable because device is locked | blocker if no safe unattended recovery |
| `DEVELOPER_MODE_REQUIRED` | Developer service cannot be used | blocker |
| `SIGNING_FAILURE` | Development signature/provisioning invalid | blocker; do not touch renderer |
| `INSTALL_UNKNOWN` | install command timed out/connection dropped before result | query installed build and reconcile |
| `AGENT_UNREACHABLE` | Amethyst runs but v2 response channel cannot be observed | relaunch/reprobe; collect app logs |
| `STALE_DEBUGSERVER` | stale debugserver/session state (including known E96-style cases) | tear down local forwarding and recreate on a new port |
| `JIT_PROCESSOR_UNCONFIGURED` | required host UniversalJIT26 processor is absent | blocker with exact configuration requirement |
| `JIT_ATTACH_FAILURE` | debugger transport/attach failed | bounded transport rebuild |
| `JIT_MODE_INSUFFICIENT` | attached/debugged but executable RX mapping cannot be established | use UniversalJIT26-capable path; no renderer changes |
| `JIT_VERIFICATION_FAILURE` | processor ran but positive mapping evidence was not observed | collect processor/app logs and fail closed |
| `DYLD_VALIDATION_FAILURE` | unsigned runtime/dylib load blocked or bypass proof lost | restore persistent attach/bypass for current generation |
| `RUNTIME_ABI_PRECHECK_FAILED` | Java/LWJGL/SPVC/classes/methods/natives incompatible before launch | fix packaged compatibility/runtime contract |
| `AMETHYST_CRASH` | native launcher/app crash | collect/symbolicate crash; first failing frame owns diagnosis |
| `AMETHYST_JETSAM` | iOS terminated process for memory pressure | collect Jetsam/system memory evidence |
| `JVM_CRASH` | hs_err/native JVM failure | collect hs_err + native logs |
| `JAVA_EXCEPTION` | uncaught Java/bootstrap exception | classify dependency/mod/classloader first |
| `NATIVE_DYLIB_LOAD_FAILURE` | expected native library missing/incompatible/unloadable | inspect ABI/signature/path; do not blame JIT after JIT proof |
| `MOD_LOADER_FAILURE` | Fabric/mod bootstrap failure | fix version/mod contract |
| `RENDERER_INIT_FAILURE` | Mithril/renderer initialization failed | renderer diagnostics/ABI probe |
| `HOT_PAYLOAD_PROVENANCE_MISMATCH` | staged renderer bytes are valid but runtime EGL/GL symbols do not resolve to that exact image | fail closed; fix loader/image identity before renderer semantics |
| `METAL_VALIDATION_FAILURE` | Metal API/resource/lifetime validation error | collect Metal diagnostics |
| `MC_READY_TIMEOUT` | launch accepted but menu/expected MC readiness never observed | collect event/log/crash evidence, classify first missing stage |
| `WORLD_LOAD_TIMEOUT` | menu ready but deterministic world never reached ready | collect world/chunk/log evidence |
| `GRAPHICS_REGRESSION` | fixed screenshot/checkpoint differs beyond gate | reject candidate or investigate visual semantics |
| `PERFORMANCE_REGRESSION` | correctness passes but benchmark regresses | reject/rework candidate |
| `RETRY_REQUIRES_CHANGED_EVIDENCE` | same device-confirmed experiment produced the same failure twice | change candidate/evidence, perform observed recovery, or provide an explicit diagnostic retry reason |
| `UNKNOWN` | evidence insufficient | collect more evidence; never guess a successful stage |

## Required invalidation

A process-generation change always clears all process/session proof. A new or ended `game_session_generation` also clears `JIT_*`, `DYLD_*`, JVM, renderer, menu, world, chunks, and benchmark readiness even when the Amethyst PID is unchanged. A new run never inherits PASS evidence from an old run/session.
