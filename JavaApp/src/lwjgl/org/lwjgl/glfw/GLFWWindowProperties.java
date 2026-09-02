package org.lwjgl.glfw;

import java.util.*;
import net.kdt.pojavlaunch.Tools;

public class GLFWWindowProperties {
    public int width, height;
    public float x, y;
    public CharSequence title;
    public boolean shouldClose, isInitialSizeCalled, isCursorEntered;
    public long monitor;
    public Map<Integer, Integer> inputModes = new HashMap<>();
    public Map<Integer, Integer> windowAttribs = new HashMap<>();

    public GLFWWindowProperties() {
        inputModes.put(GLFW.GLFW_CURSOR, GLFW.GLFW_CURSOR_NORMAL);
        inputModes.put(GLFW.GLFW_STICKY_KEYS, GLFW.GLFW_FALSE);
        inputModes.put(GLFW.GLFW_STICKY_MOUSE_BUTTONS, GLFW.GLFW_FALSE);
        inputModes.put(GLFW.GLFW_LOCK_KEY_MODS, GLFW.GLFW_FALSE);
        inputModes.put(GLFW.GLFW_RAW_MOUSE_MOTION, GLFW.GLFW_FALSE);
        inputModes.put(GLFW.GLFW_UNLIMITED_MOUSE_BUTTONS, GLFW.GLFW_FALSE);
        inputModes.put(GLFW.GLFW_IME, GLFW.GLFW_FALSE);
    }
    
    @Override
    public String toString() {
        return "width=" + width + ", " +
          "height=" + height + ", " +
          "x=" + x + ", " +
          "y=" + y + ", ";
    }
}