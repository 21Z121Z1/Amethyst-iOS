# Minecraft lab probe

This directory defines the test-only Java side of the unattended physical-device contract. It is deliberately separate from gameplay/renderer code: the probe reports facts; it does not approximate missing graphics semantics or alter production behavior.

## Run identity

Before Amethyst launches Minecraft, `amethystd` atomically writes the current run ID to:

```text
${user.home}/agent-lab/current-run-id
```

A test mod or version-specific instrumentation layer calls `LabEvents.emit(...)`. Events are appended to:

```text
${user.home}/agent-lab/<run_id>.jsonl
```

The host accepts only `amethyst-lab/v1` events whose `run_id` matches the active run. A previous run can therefore never make a new run pass accidentally.

## Required hook semantics

Version-specific test integration should call the helper only at authoritative lifecycle boundaries:

- `jvm_ready`: test instrumentation is executing inside the target JVM;
- `minecraft_bootstrap`: Minecraft client bootstrap has begun;
- `renderer_loading`: renderer/backend initialization has actually begun;
- `renderer_ready`: the selected renderer finished its required initialization without fallback;
- `menu_ready`: the actual title/menu screen is ready for interaction;
- `world_loading`: deterministic test world/server load began;
- `world_ready`: client is in the requested world and the test harness can observe the target player/camera state;
- `chunks_stable`: the benchmark's required chunk set has reached its stability criterion;
- benchmark events: exact warmup/measurement boundaries.

Do not derive these from fixed sleeps, screenshot appearance, app `game_running`, or process existence. If a version-specific hook does not exist yet, leave that stage unproven; `amethystctl run smoke` must time out/fail instead of fabricating success.

`LabEvents.java` has no Fabric/Minecraft dependency and can be copied/compiled into the dedicated test mod. The version-specific adapter is intentionally small so mappings/API churn remains isolated from the Amethyst host harness.
