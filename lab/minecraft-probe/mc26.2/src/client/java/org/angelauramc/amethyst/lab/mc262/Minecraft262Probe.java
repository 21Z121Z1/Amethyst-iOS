package org.angelauramc.amethyst.lab.mc262;

import java.io.IOException;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientChunkEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.fabricmc.fabric.api.client.screen.v1.ScreenEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.multiplayer.ClientLevel;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.phys.Vec3;

import org.angelauramc.amethyst.lab.LabEvents;
import org.lwjgl.opengl.GL;
import org.lwjgl.system.FunctionProvider;
import org.lwjgl.system.SharedLibrary;

/** Minecraft 26.2 semantic adapter. It emits facts only from authoritative Fabric/Minecraft hooks. */
public final class Minecraft262Probe implements ClientModInitializer {
    private static final Pattern HOT_STAGE = Pattern.compile(
        "(?:^|/)agent-payloads/\\.staging/([0-9a-fA-F]{64})/(?:.*)$"
    );

    private final AtomicBoolean menuReady = new AtomicBoolean();
    private final AtomicBoolean rendererConsumerReady = new AtomicBoolean();
    private final AtomicBoolean rendererConsumerFailed = new AtomicBoolean();
    private final AtomicBoolean playJoined = new AtomicBoolean();
    private final AtomicBoolean worldLoading = new AtomicBoolean();
    private final AtomicBoolean worldReady = new AtomicBoolean();
    private final AtomicBoolean chunksStable = new AtomicBoolean();
    private final AtomicLong chunkMutationSerial = new AtomicLong();
    private final AtomicReference<Object> titleScreen = new AtomicReference<>();
    private int readyTicks;
    private int stableChunkTicks;
    private long lastChunkMutationSerial = -1;

    @Override
    public void onInitializeClient() {
        emit("jvm_ready", Map.of("adapter", "mc26.2-fabric"));

        // Do not call GL.getFunctionProvider() from the initializer itself. That
        // can initialize LWJGL earlier than Minecraft normally would and turn a
        // provenance probe into a behavior-changing experiment. Observe it only
        // after Minecraft reaches its normal client lifecycle/tick path.
        ClientLifecycleEvents.CLIENT_STARTED.register(client -> {
            emit("minecraft_bootstrap", Map.of("minecraft_version", "26.2"));
            emitRendererConsumerIdentity("client_started");
        });

        ScreenEvents.AFTER_INIT.register((client, screen, width, height) -> {
            if (!(screen instanceof TitleScreen)) return;
            titleScreen.set(screen);
            ScreenEvents.afterExtract(screen).register((renderedScreen, graphics, mouseX, mouseY, delta) -> {
                if (renderedScreen == titleScreen.get() && client.gui.screen() == renderedScreen && menuReady.compareAndSet(false, true)) {
                    emit("menu_ready", Map.of(
                        "screen", renderedScreen.getClass().getName(),
                        "scaled_width", width,
                        "scaled_height", height,
                        "proof", "title_screen_after_extract"
                    ));
                }
            });
        });

        ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> {
            playJoined.set(true);
            readyTicks = 0;
            stableChunkTicks = 0;
            chunksStable.set(false);
            lastChunkMutationSerial = chunkMutationSerial.get();
            if (worldLoading.compareAndSet(false, true)) {
                emit("world_loading", Map.of("proof", "client_play_connection_join"));
            }
        });

        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> {
            playJoined.set(false);
            worldLoading.set(false);
            worldReady.set(false);
            chunksStable.set(false);
            readyTicks = 0;
            stableChunkTicks = 0;
        });

        ClientChunkEvents.CHUNK_LOAD.register(new ClientChunkEvents.Load() {
            @Override
            public void onChunkLoad(ClientLevel level, LevelChunk chunk) {
                chunkMutationSerial.incrementAndGet();
            }
        });
        ClientChunkEvents.CHUNK_UNLOAD.register(new ClientChunkEvents.Unload() {
            @Override
            public void onChunkUnload(ClientLevel level, LevelChunk chunk) {
                chunkMutationSerial.incrementAndGet();
            }
        });

        ClientTickEvents.END_CLIENT_TICK.register(this::onEndClientTick);
    }

    private void emitRendererConsumerIdentity(String phase) {
        if (rendererConsumerReady.get() || rendererConsumerFailed.get()) return;
        try {
            String requested = System.getProperty("org.lwjgl.opengl.libname", "");
            FunctionProvider provider = GL.getFunctionProvider();
            if (provider == null) return;

            long compileShader = provider.getFunctionAddress("glCompileShader");
            long drawElements = provider.getFunctionAddress("glDrawElements");
            String loaded = provider instanceof SharedLibrary sharedLibrary ? sharedLibrary.getPath() : null;
            String canonicalRequested = canonicalPath(requested);
            String canonicalLoaded = canonicalPath(loaded);
            String requestedDigest = hotDigest(canonicalRequested);
            String loadedDigest = hotDigest(canonicalLoaded);
            boolean hotRequested = requestedDigest != null;
            boolean pathMatches = canonicalRequested != null && canonicalRequested.equals(canonicalLoaded);
            boolean addressesReady = compileShader != 0L && drawElements != 0L;
            boolean provenanceOk = addressesReady && (!hotRequested || (pathMatches && requestedDigest.equals(loadedDigest)));

            Map<String, Object> fields = new HashMap<>();
            fields.put("proof", "lwjgl_gl_function_provider");
            fields.put("phase", phase);
            fields.put("provider_class", provider.getClass().getName());
            fields.put("requested_library_path", canonicalRequested == null ? requested : canonicalRequested);
            fields.put("loaded_library_path", canonicalLoaded == null ? (loaded == null ? "<unavailable>" : loaded) : canonicalLoaded);
            fields.put("candidate_digest", hotRequested ? requestedDigest : "bundled");
            fields.put("provenance_ok", provenanceOk);
            fields.put("glCompileShader", Long.toUnsignedString(compileShader));
            fields.put("glDrawElements", Long.toUnsignedString(drawElements));

            if (provenanceOk && rendererConsumerReady.compareAndSet(false, true)) {
                emit("renderer_ready", fields);
            } else if (hotRequested && rendererConsumerFailed.compareAndSet(false, true)) {
                fields.put("failure_class", "HOT_PAYLOAD_PROVENANCE_MISMATCH");
                fields.put("reason", "lwjgl_function_provider_does_not_match_requested_staged_image");
                emit("failed", fields);
            }
        } catch (Throwable error) {
            System.err.println("[AmethystLab] LWJGL consumer provenance unavailable at " + phase + ": " + error);
        }
    }

    private static String canonicalPath(String value) {
        if (value == null || value.isBlank() || !value.startsWith("/")) return value;
        try {
            return Path.of(value).toRealPath().toString();
        } catch (IOException ignored) {
            return Path.of(value).toAbsolutePath().normalize().toString();
        }
    }

    private static String hotDigest(String path) {
        if (path == null) return null;
        Matcher matcher = HOT_STAGE.matcher(path);
        return matcher.find() ? matcher.group(1).toLowerCase() : null;
    }

    private void onEndClientTick(Minecraft client) {
        emitRendererConsumerIdentity("client_tick");
        Entity playerEntity = client.player;
        Entity cameraEntity = client.getCameraEntity();
        boolean semanticWorld = playJoined.get()
            && client.level != null
            && playerEntity != null
            && cameraEntity != null;
        readyTicks = semanticWorld ? readyTicks + 1 : 0;

        if (readyTicks >= 3 && worldReady.compareAndSet(false, true)) {
            Vec3 playerPosition = playerEntity.position();
            Vec3 cameraPosition = cameraEntity.position();
            emit("world_ready", Map.of(
                "proof", "play_join_plus_three_client_ticks",
                "player_x", playerPosition.x,
                "player_y", playerPosition.y,
                "player_z", playerPosition.z,
                "camera_x", cameraPosition.x,
                "camera_y", cameraPosition.y,
                "camera_z", cameraPosition.z,
                "camera_entity", cameraEntity.getClass().getName()
            ));
        }

        if (!worldReady.get()) return;
        long serial = chunkMutationSerial.get();
        if (serial == lastChunkMutationSerial) stableChunkTicks++;
        else {
            lastChunkMutationSerial = serial;
            stableChunkTicks = 0;
        }
        if (stableChunkTicks >= 20 && chunksStable.compareAndSet(false, true)) {
            emit("chunks_stable", Map.of(
                "proof", "20_client_ticks_without_chunk_load_or_unload",
                "stable_ticks", stableChunkTicks,
                "chunk_mutation_serial", serial
            ));
        }
    }

    private static void emit(String event, Map<String, ?> fields) {
        try {
            LabEvents.emit(event, fields);
        } catch (IOException error) {
            System.err.println("[AmethystLab] failed to emit " + event + ": " + error);
        }
    }
}
