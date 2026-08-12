# UniversalJIT26 host contract

The repository contains the host breakpoint processor used successfully during the August 2026 Amethyst physical-device debugging rollout: `tools/amethystd/universal_jit26_processor.py`.

## Ordering invariant

The host must not wait for executable-mapping proof before requesting Minecraft launch. The UniversalJIT26 breakpoints that allocate the JVM mapping are reached by `launchJVM`, so the proven ordering is:

```text
start debugserver forwarding
-> attach UniversalJIT26 processor to current Amethyst process generation
-> observe processor-attached proof
-> submit profile / launch request
-> catch 0x69 and 0xf00d protocol breakpoints
-> observe host RX allocation/page preparation
-> observe Amethyst latestlog mapping at RW=..., RX=...
-> when external dylibs are required, observe both DyldLVBypass hook successes
-> declare JIT_RX_MAPPING_OK / DYLD_BYPASS_READY
```

Debugger attachment alone is never executable-JIT proof.

## RSP processor

The processor implements only the protocol Amethyst's UniversalJIT26 consumer needs:

- no-ack negotiation and `vAttach`;
- BRK `0x69` Universal sentinel and RX allocation;
- BRK `0xf00d` commands for region preparation, extension acceptance, detach policy, and patch preparation;
- process-lifetime attachment when requested by Amethyst.

It emits stable host proof markers consumed by `amethystd`:

- `AMETHYST_JIT_PROCESSOR_ATTACHED`
- `AMETHYST_JIT_UNIVERSAL_HANDSHAKE_OK`
- `AMETHYST_JIT_EXTENSION_ACCEPTED`
- `AMETHYST_JIT_KEEP_ATTACHED value=true|false`
- `AMETHYST_JIT_HOST_EXEC_READY`
- `AMETHYST_JIT_PATCH_REGION_READY`

These markers prove host-side protocol effects only. `amethystd` additionally checks new bytes in the app's `latestlog.txt` for the app-side mapping and Dyld hook proof, so a host-side command cannot falsely promote the run by itself.

## Session lifetime

A process generation change invalidates the session. A failed run, explicit stop, next run, or daemon shutdown terminates the processor and debugserver. Successful runs keep the session alive until the game is stopped so unsigned/external dylib loads remain covered by the attached debugger path.

`AMETHYST_JIT_PROCESSOR` remains an optional escape hatch for development, but the repository implementation is now the default and is the only implementation covered by repository tests.
