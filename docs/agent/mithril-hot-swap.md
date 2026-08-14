# Mithril hot swap

This workflow replaces `libmithril.dylib` without rebuilding or reinstalling Amethyst. It deliberately **does not** replace a renderer inside a live JVM. Every swap or rollback starts a fresh Amethyst process and therefore a fresh JLI/JVM/LWJGL/renderer lifetime.

## Codex entry point

```bash
./tools/amethystctl mithril swap /absolute/path/to/libmithril.dylib \
  --profile mc26.2-directvulkan \
  --target RENDERER_READY
```

Optional stronger gates are `MENU_READY`, `WORLD_READY`, and `CHUNKS_STABLE`.

Inspect current state and the bounded transaction ledger:

```bash
./tools/amethystctl mithril status
```

Undo the latest transition for the currently active digest:

```bash
./tools/amethystctl mithril rollback \
  --profile mc26.2-directvulkan \
  --target RENDERER_READY
```

An exact staged digest can be selected explicitly:

```bash
./tools/amethystctl mithril rollback \
  --digest <64-character-sha256> \
  --profile mc26.2-directvulkan
```

Use `--digest bundled` to return to the app-bundled Mithril.

## Transaction contract

A `swap` is accepted only when all of the following hold:

1. The local input is exactly a regular file named `libmithril.dylib`.
2. Existing payload state is inspected and recorded before staging.
3. The normal Agent v3.1 staging path verifies the Mach-O code signature and uploads into a content-addressed `.staging/<digest>` directory.
4. The device-active pointer is inspected again and must equal the newly staged digest before launch.
5. `run_smoke` performs the normal CoreDevice `--terminate-existing` launch, so the test gets a fresh Amethyst/JLI/JVM lifetime.
6. If a previous `process_generation` exists, the new run must have a different generation.
7. The smoke must emit the authoritative Fabric `renderer_ready` event from the actual LWJGL `GL.getFunctionProvider()` consumer.
8. `candidate_digest`, staged path, `glCompileShader`, and `glDrawElements` must all prove the same candidate.
9. Only then is the transaction recorded as `passed` / last-known-good.

A failed candidate remains active by default so its crash/log evidence remains reproducible. The command returns the previous digest and a rollback command instead of silently changing the experiment.

## Rollback contract

Rollback never trusts a branch name, local path, or conversation memory. It chooses the previous digest from the persisted transition ledger (or an explicit `--digest`) and then verifies the preserved device stage before switching:

- manifest identity and digest;
- safe relative paths;
- every staged file size and SHA-256;
- verified code-signature provenance for `libmithril.dylib`.

Only after verification does it update the active pointer. If the state-changing AFC operation reports a transport error after the rename, the tool **does not replay the write**; it performs a fresh read/inspect and accepts the transaction only if the requested digest is already authoritative on-device. It then cold-restarts and performs the same LWJGL consumer proof as `swap`.

## Why this is cold restart rather than live `dlclose` / `dlopen`

Mithril is the GL function provider for a JVM that caches native function addresses and owns renderer state (GL contexts, shaders/programs, buffers, textures, FBOs, Vulkan/Metal objects and compiler state). Replacing that provider inside the same JVM would leave cached addresses and renderer objects tied to the old image. Apple dynamic-loader semantics also do not promise that a `dlclose` immediately removes an image once a caller releases a handle. The safe experimental unit is therefore: content-addressed dylib switch, then a fresh process.
