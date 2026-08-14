# Unattended device harness architecture

## Layers

```text
Codex / human operator
        |
 tools/amethystctl    stable wrapper + JSON CLI
        |
   amethystd          authoritative long-lived supervisor
        |
   +-- pymobiledevice3 / House Arrest / DVT / debugserver transport
   +-- bundled UniversalJIT26 RSP processor
   +-- xcrun devicectl / xcodebuild / xctrace where appropriate
        |
      physical iPad
        |
   Amethyst Agent v2
        |
   UniversalJIT26 / JVM / renderer
        |
      Minecraft
        |
 MC26.2 test-only Fabric semantic probe
```

### Codex

Owns reasoning: choose the next first failing invariant, patch one responsible layer, compare evidence, accept/reject, remove temporary diagnostics, commit/push an accepted change, and continue. It is not the device daemon and should not infer semantic state from screenshots/F3.

### Stable host runtime

`tools/bootstrap-agent` creates a supported Python 3.12/3.13 harness venv. `tools/amethystctl` is the stable executable entrypoint. Persistent state stays under `.amethyst-agent`, but the Unix socket uses a short hashed runtime directory under `/tmp`, so deep Codex worktree paths cannot exceed AF_UNIX path limits. Daemon health includes interpreter identity; a CLI/daemon Python mismatch is explicit and recoverable with `daemon restart`.

### `amethystd`

Owns selected device/bundle, host and game-session generations, candidate identity, debugserver/JIT processor lifetime, request/response transport, retries, classification, and artifacts. Its on-disk state is the host source of truth across CLI calls, but device active-payload metadata is authoritative over stale host candidate cache.

### Amethyst Agent v2

Owns app-side domain semantics: status, profile selection, launch/terminate, process/game-session identity, input primitives, probes and append-only events. It does not grant itself JIT. Launch requests must carry the host-generated `game_session_generation`, which is echoed in responses/events and checked by the host.

## Two-level lifetime identity

PID alone is insufficient, and one `process_generation` is still insufficient when the same Amethyst host process launches Minecraft more than once.

```text
Amethyst host process_generation A
  ├─ Minecraft game_session_generation 1
  │    └─ JIT attach generation 1
  └─ Minecraft game_session_generation 2
       └─ JIT attach generation 2
```

A host identity change invalidates everything below it. A new/end game session invalidates JIT, Dyld, JVM, renderer, menu, world, chunk and benchmark proof even if PID/process generation are unchanged. UniversalJIT26 attach identity includes the game session, preventing stale debugserver/JIT proof from being reused across launches.

## Observed success

The harness distinguishes request acceptance from observed state:

- debugserver connected != executable JIT mapping established;
- staged SHA verified != Minecraft is calling that staged dylib;
- launch accepted != JVM started;
- `game_running` != Minecraft menu/world ready;
- panorama/frame motion != GUI ready or world loaded;
- install timeout != install failure.

## Hot payload identity and provenance

Renderer payloads are immutable content-addressed stages plus an active pointer. The host verifies transfer bytes, and the app re-verifies pointer/manifest/file size/SHA before use.

For a hot Mithril payload, Amethyst now resolves the staged absolute path **before** setting `org.lwjgl.opengl.libname`; LWJGL receives that absolute path rather than the bare bundled name. Before `renderer_ready`, native provenance resolves representative EGL/GL symbols with `dladdr` and requires them to come from the same expected staged Mach-O path. A mismatch fails closed as `HOT_PAYLOAD_PROVENANCE_MISMATCH` instead of permitting duplicate bundled/staged renderer images.

`payload clear` removes only the active pointer. Staging content remains for forensic comparison/re-activation and is never recursively deleted by that command.

## File and event transport

Agent v2 uses Documents-relative directories; host paths never persist an iOS container UUID:

```text
agent-requests/
agent-responses/
agent-events/<run_id>.jsonl
agent-processed/
agent-lab/current-run-id
agent-lab/current-session-id
agent-lab/<run_id>.jsonl
agent-payloads/.staging/<digest>/
agent-payloads/active/<name>.json
```

Requests/responses and active-pointer changes are atomic. Growing log/event files use bounded streaming reads. Every lab event includes both run and game-session identity; stale-session events cannot promote the current run.

## Minecraft semantic layer

The MC26.2 Fabric adapter builds as a real Java 25 CI target. It uses Fabric lifecycle/screen/play/chunk events to distinguish bootstrap, real TitleScreen readiness, play JOIN, player/camera world readiness and chunk stability. Screenshot/frame metrics remain useful graphics evidence but cannot synthesize semantic readiness.

## Retry and causal experiment control

Each smoke attempt is keyed by device-confirmed candidate digest + profile + target + dynamic-dylib requirement. Stable failure fingerprints normalize volatile PIDs/ports/addresses. After the same unchanged failure occurs twice, a third identical attempt is blocked as `RETRY_REQUIRES_CHANGED_EVIDENCE` unless Codex supplies an explicit diagnostic retry reason. Candidate changes, observed lifecycle recovery, or a named diagnostic deviation reset/justify the loop.

## Artifacts

Every physical run collects run-scoped state/events/logs and failure evidence. CI separately uploads host contract/provenance data, the built MC26.2 probe, and native build metadata/binary. CI proves that the harness and native control plane compile and satisfy their contracts; only a fresh physical-iPad run can prove real device JIT, GUI/world correctness and performance.
