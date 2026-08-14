package org.angelauramc.amethyst.lab;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;

/** Test-only append-only event writer. Contains no Minecraft/Fabric dependency. */
public final class LabEvents {
    private static final Pattern SAFE_RUN_ID = Pattern.compile("[A-Za-z0-9_-]{1,96}");
    private static final Object LOCK = new Object();

    private LabEvents() {}

    public static String currentRunId() throws IOException {
        Path file = Path.of(System.getProperty("user.home"), "agent-lab", "current-run-id");
        String value = Files.readString(file, StandardCharsets.UTF_8).trim();
        if (!SAFE_RUN_ID.matcher(value).matches()) {
            throw new IOException("invalid Amethyst agent run id");
        }
        return value;
    }

    public static String currentGameSessionGeneration() throws IOException {
        Path file = Path.of(System.getProperty("user.home"), "agent-lab", "current-session-id");
        String value = Files.readString(file, StandardCharsets.UTF_8).trim();
        if (!SAFE_RUN_ID.matcher(value).matches()) {
            throw new IOException("invalid Amethyst game session generation");
        }
        return value;
    }

    public static void emit(String event) throws IOException {
        emit(event, Map.of());
    }

    public static void emit(String event, Map<String, ?> fields) throws IOException {
        if (event == null || event.isBlank()) {
            throw new IllegalArgumentException("event must not be blank");
        }
        String runId = currentRunId();
        String gameSessionGeneration = currentGameSessionGeneration();
        Path directory = Path.of(System.getProperty("user.home"), "agent-lab");
        Files.createDirectories(directory);
        Path output = directory.resolve(runId + ".jsonl");

        StringBuilder json = new StringBuilder(256);
        json.append('{');
        field(json, "protocol", "amethyst-lab/v1");
        json.append(',');
        field(json, "run_id", runId);
        json.append(',');
        field(json, "game_session_generation", gameSessionGeneration);
        json.append(',');
        field(json, "event_id", UUID.randomUUID().toString());
        json.append(',');
        field(json, "event", event);
        json.append(",\"timestamp_ms\":").append(Instant.now().toEpochMilli());
        for (Map.Entry<String, ?> entry : fields.entrySet()) {
            if (entry.getKey() == null || entry.getKey().isBlank()) continue;
            json.append(',');
            fieldName(json, entry.getKey());
            appendValue(json, entry.getValue());
        }
        json.append("}\n");

        synchronized (LOCK) {
            Files.writeString(
                output,
                json.toString(),
                StandardCharsets.UTF_8,
                StandardOpenOption.CREATE,
                StandardOpenOption.WRITE,
                StandardOpenOption.APPEND
            );
        }
    }

    private static void field(StringBuilder out, String name, String value) {
        fieldName(out, name);
        quoted(out, value);
    }

    private static void fieldName(StringBuilder out, String name) {
        quoted(out, name);
        out.append(':');
    }

    private static void appendValue(StringBuilder out, Object value) {
        if (value == null) {
            out.append("null");
        } else if (value instanceof Number || value instanceof Boolean) {
            out.append(value);
        } else {
            quoted(out, String.valueOf(value));
        }
    }

    private static void quoted(StringBuilder out, String value) {
        out.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) out.append(String.format("\\u%04x", (int)c));
                    else out.append(c);
                }
            }
        }
        out.append('"');
    }
}
