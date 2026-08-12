# Unattended device harness architecture

## Layers

```text
Codex / human operator
        |
   amethystctl        stable JSON client
        |
   amethystd          authoritative long-lived supervisor
        |
   +-- pymobiledevice3 / House Arrest / DVT / debugserver transport
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
   optional Fabric lab probe
```

### Codex

Owns reasoning: choose the next goal, interpret evidence, patch code, compare baseline/candidate, and decide accept/reject. It must not become the device daemon.

### `amethystd`

Owns long-lived deterministic state: selected device/bundle, process generation, debugserver/JIT processor lifetime, request/response transport, run IDs, retries, classification, and artifacts. Its on-disk state is the host source of truth across individual `amethystctl` invocations.

### `amethystctl`

A small synchronous JSON CLI. Stdout is one JSON object suitable for an agent. Human diagnostics go to stderr. Routine automation should call it instead of composing raw `pymobiledevice3`, `devicectl`, PID, port, plist, or sleep commands.

### pymobiledevice3 / CoreDevice

The harness reuses current device services instead of reimplementing usbmux/AFC/House Arrest/DVT. Classic container transport uses House Arrest over USB. Developer services use the currently supported pymobiledevice3/CoreDevice path.

### Amethyst Agent v2

App-side domain semantics only: status, profile selection, launch/terminate, probes, process identity, append-only events, and idempotent responses. It does not try to grant itself JIT.

## Process generation

PID alone is not enough because stale observations can survive an app restart. Agent v2 exposes an opaque per-process `process_generation` value. When either PID or generation changes, host state invalidates:

- executable-JIT proof;
- persistent debugger/Dyld-bypass proof;
- JVM readiness;
- renderer readiness;
- Minecraft menu/world readiness.

## Observed success

The harness distinguishes request acceptance from observed state. Examples:

- debugserver connected != UniversalJIT26 RX/RW mapping established;
- file transfer returned != staged payload hash/manifest verified;
- launch request accepted != JVM started;
- app reports game surface running != Minecraft menu/world ready;
- install command timed out != install failed.

## File transport

Agent v2 uses Documents-relative directories so it never persists container UUID paths:

```text
agent-requests/
agent-responses/
agent-events/<run_id>.jsonl
agent-processed/
agent-payloads/.staging/
agent-payloads/active/
```

Host writes a request atomically (`.tmp` then rename). The app claims/processes it and writes an atomic response. Repeating a `request_id` returns/reuses the existing response rather than repeating a state-changing action.

## UniversalJIT26 ownership

The repository currently contains the app-side UniversalJIT26 consumer but not a verified general host-side breakpoint processor implementation. The supervisor therefore treats that processor as an explicit configured dependency and fails closed when it is missing. It may own/debugserver transport and processor lifetime, but it may not infer `JIT_READY` from attach success.

Positive evidence is separated into:

- `exec_ready`: observed UniversalJIT26 mapping handshake (for example the known RX/RW mapping markers);
- `dynamic_library_load_ready`: observed persistent-attached/Dyld library-validation bypass marker.

## Hot experiment payloads

The low-frequency AgentDebug base app should contain the stable launcher/JIT/control foundation. High-frequency renderer/mod/config experiments should be staged into Documents, verified by manifest/hash, then atomically activated. This prevents a full hundreds-of-megabytes IPA reinstall for every Mithril/MetalUniversal iteration.

## Benchmark layer

A test-only Fabric lab probe should eventually emit explicit readiness and benchmark events (`menu_ready`, `world_ready`, `chunks_stable`, warmup/measurement boundaries) and in-game frame metrics. Device-side DVT/sysmon and optional Instruments traces complement, rather than replace, in-game frame-time evidence.
