# Minecraft lab probe

This directory defines the test-only Java side of the unattended physical-device contract. It is deliberately separate from gameplay/renderer code: the probe reports authoritative lifecycle facts; it does not approximate missing graphics semantics or alter production renderer behavior.

## Run and game-session identity

Before Amethyst requests Minecraft launch, `amethystd` atomically writes both identities:

```text
${user.home}/agent-lab/current-run-id
${user.home}/agent-lab/current-session-id
```

`LabEvents.emit(...)` includes both `run_id` and `game_session_generation` in every `amethyst-lab/v1` event and appends to:

```text
${user.home}/agent-lab/<run_id>.jsonl
```

The host accepts an event only when both identities match the active run. Returning to the Amethyst launcher ends the game session and invalidates JVM/JIT/renderer/menu/world proof even if the Amethyst host PID did not change.

## Minecraft 26.2 adapter

`mc26.2/` is a dedicated Fabric test mod compiled against Minecraft 26.2, Fabric Loader 0.19.3 and the current 26.2 Fabric API. It intentionally uses public Fabric lifecycle events instead of screenshot classification or fixed delays.

The adapter emits:

- `jvm_ready` when its client initializer executes inside the target JVM;
- `minecraft_bootstrap` from the Fabric client-start lifecycle;
- `menu_ready` only after a real `TitleScreen` has completed init and a subsequent `ScreenEvents.afterExtract` callback has run;
- `world_loading` from a real client play connection JOIN;
- `world_ready` only after JOIN and three consecutive client ticks with non-null level, player and camera entity state;
- `chunks_stable` only after `world_ready` and a continuous tick window with no client chunk load/unload mutations.

This deliberately makes a title panorama insufficient for `WORLD_READY`: it can satisfy neither JOIN nor player/camera invariants.

Build the adapter with Java 25:

```sh
cd lab/minecraft-probe/mc26.2
gradle --no-daemon build
```

Install the resulting remapped JAR into the dedicated test instance used by the AgentDebug profile. The host CI builds this JAR on every harness change so Fabric API/mapping drift fails before a physical-device run.

## Required hook semantics

Version-specific integration must emit only authoritative boundaries:

- `jvm_ready`: test instrumentation executes inside the target JVM;
- `minecraft_bootstrap`: Minecraft client bootstrap has begun;
- `renderer_loading`: renderer/backend initialization has actually begun;
- `renderer_ready`: selected renderer completed initialization and its runtime provenance passed;
- `menu_ready`: actual title/menu UI lifecycle is ready;
- `world_loading`: deterministic play connection/world load began;
- `world_ready`: the requested world has observable player/camera state;
- `chunks_stable`: the benchmark's required chunk-stability criterion is satisfied;
- benchmark events: exact warmup/measurement boundaries.

Do not derive these states from fixed sleeps, screenshot appearance, F3, app `game_running`, process existence, or a moving panorama. If a hook is unavailable, leave that stage unproven; `amethystctl run smoke` must time out/fail closed rather than fabricate success.
