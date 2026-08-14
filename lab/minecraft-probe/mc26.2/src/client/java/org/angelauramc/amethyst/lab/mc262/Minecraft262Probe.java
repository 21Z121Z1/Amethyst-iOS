package org.angelauramc.amethyst.lab.mc262;

import java.io.IOException;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

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

/** Minecraft 26.2 semantic adapter. It emits facts only from authoritative Fabric/Minecraft hooks. */
public final class Minecraft262Probe implements ClientModInitializer {
    private final AtomicBoolean menuReady = new AtomicBoolean();
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

        ClientLifecycleEvents.CLIENT_STARTED.register(client ->
            emit("minecraft_bootstrap", Map.of("minecraft_version", "26.2"))
        );

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

    private void onEndClientTick(Minecraft client) {
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
