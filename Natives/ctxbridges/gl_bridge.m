#import <Foundation/Foundation.h>
#import "SurfaceViewController.h"

#include <dlfcn.h>
#include <string.h>
#include "bridge_tbl.h"
#include "environ.h"
#include "gl_bridge.h"
#include "utils.h"

static EGLDisplay g_EglDisplay;
static egl_library handle;

static bool dlsym_EGL() {
    NSString *renderer = NSProcessInfo.processInfo.environment[@"POJAV_RENDERER"];
    const char *egl_library_path = [renderer isEqualToString:@ RENDERER_NAME_MITHRIL]
        ? "@rpath/libmithril.dylib"
        : "@rpath/libtinygl4angle.dylib";

    void* dl_handle = dlopen(egl_library_path, RTLD_NOW | RTLD_GLOBAL);
    if (!dl_handle) {
        NSLog(@"EGLBridge: failed to load %s: %s", egl_library_path, dlerror());
        return false;
    }

    memset(&handle, 0, sizeof(handle));
#define LOAD_EGL(NAME)                                                        \
    do {                                                                      \
        handle.NAME = dlsym(dl_handle, #NAME);                                \
        if (!handle.NAME) {                                                   \
            NSLog(@"EGLBridge: %s is missing %s", egl_library_path, #NAME); \
            return false;                                                     \
        }                                                                     \
    } while (0)
    LOAD_EGL(eglBindAPI);
    LOAD_EGL(eglChooseConfig);
    LOAD_EGL(eglCreateContext);
    LOAD_EGL(eglCreateWindowSurface);
    LOAD_EGL(eglDestroyContext);
    LOAD_EGL(eglDestroySurface);
    LOAD_EGL(eglGetConfigAttrib);
    LOAD_EGL(eglGetCurrentContext);
    LOAD_EGL(eglGetDisplay);
    LOAD_EGL(eglGetError);
    LOAD_EGL(eglGetPlatformDisplay);
    LOAD_EGL(eglInitialize);
    LOAD_EGL(eglMakeCurrent);
    LOAD_EGL(eglSwapBuffers);
    LOAD_EGL(eglReleaseThread);
    LOAD_EGL(eglSwapInterval);
    LOAD_EGL(eglTerminate);
    LOAD_EGL(eglGetCurrentSurface);
#undef LOAD_EGL
    return true;
}

static bool gl_init() {
    if (!dlsym_EGL()) return false;

    g_EglDisplay = handle.eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (g_EglDisplay == EGL_NO_DISPLAY) {
        NSDebugLog(@"EGLBridge: eglGetDisplay(EGL_DEFAULT_DISPLAY) returned EGL_NO_DISPLAY");
        return false;
    }
    if (!handle.eglInitialize(g_EglDisplay, NULL, NULL)) {
        NSDebugLog(@"EGLBridge: Error eglInitialize() failed: 0x%x", handle.eglGetError());
        return false;
    }
    return true;
}

gl_render_window_t* gl_init_context(gl_render_window_t *share) {
    gl_render_window_t* bundle = calloc(1, sizeof(gl_render_window_t));

    NSString *renderer = NSProcessInfo.processInfo.environment[@"POJAV_RENDERER"];
    BOOL desktopGL = [renderer isEqualToString:@ RENDERER_NAME_MTL_ANGLE] ||
                     [renderer isEqualToString:@ RENDERER_NAME_MITHRIL];

    const EGLint attribs[] = {
        EGL_RED_SIZE, 8,
        EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8,
        EGL_ALPHA_SIZE, 8,
        EGL_DEPTH_SIZE, 24,
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT|EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, desktopGL ? EGL_OPENGL_BIT : EGL_OPENGL_ES3_BIT,
        EGL_NONE
    };

    EGLint num_configs;
    EGLint vid;
    if (!handle.eglChooseConfig(g_EglDisplay, attribs, &bundle->config, 1, &num_configs)) {
        NSDebugLog(@"EGLBridge: Error couldn't get an EGL visual config: 0x%x", handle.eglGetError());
        free(bundle);
        return NULL;
    }
    if (!bundle->config || num_configs <= 0) {
        NSDebugLog(@"EGLBridge: no matching EGL config for renderer %@", renderer);
        free(bundle);
        return NULL;
    }

    if (!handle.eglGetConfigAttrib(g_EglDisplay, bundle->config, EGL_NATIVE_VISUAL_ID, &vid)) {
        NSDebugLog(@"EGLBridge: Error eglGetConfigAttrib() failed: 0x%x", handle.eglGetError());
        free(bundle);
        return NULL;
    }

    EGLBoolean bindResult;
    if (desktopGL) {
        NSDebugLog(@"EGLBridge: Binding to desktop OpenGL");
        bindResult = handle.eglBindAPI(EGL_OPENGL_API);
    } else {
        NSDebugLog(@"EGLBridge: Binding to OpenGL ES");
        bindResult = handle.eglBindAPI(EGL_OPENGL_ES_API);
    }
    if (!bindResult) {
        NSDebugLog(@"EGLBridge: bind failed: 0x%x", handle.eglGetError());
        free(bundle);
        return NULL;
    }

    CALayer *nativeLayer = SurfaceViewController.surface.layer;
    if ([renderer isEqualToString:@ RENDERER_NAME_MITHRIL] &&
        ![nativeLayer isKindOfClass:CAMetalLayer.class]) {
        NSLog(@"EGLBridge: Mithril requires a CAMetalLayer native window");
        free(bundle);
        return NULL;
    }
    bundle->surface = handle.eglCreateWindowSurface(g_EglDisplay, bundle->config,
        (__bridge EGLNativeWindowType)nativeLayer, NULL);
    if (!bundle->surface) {
        NSDebugLog(@"EGLBridge: eglCreateWindowSurface finished with error: 0x%x", handle.eglGetError());
        free(bundle);
        return NULL;
    }

    const EGLint ctx_attribs[] = {
        EGL_CONTEXT_CLIENT_VERSION, 3,
        EGL_NONE
    };
    bundle->context = handle.eglCreateContext(g_EglDisplay, bundle->config, share ? share->context : EGL_NO_CONTEXT, ctx_attribs);
    if (!bundle->context) {
        NSDebugLog(@"EGLBridge: Error eglCreateContext finished with error: 0x%x", handle.eglGetError());
        handle.eglDestroySurface(g_EglDisplay, bundle->surface);
        free(bundle);
        return NULL;
    }

    return bundle;
}

void gl_make_current(gl_render_window_t* bundle) {
    if(!bundle) {
        if(handle.eglMakeCurrent(g_EglDisplay, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT)) {
            currentBundle = NULL;
        }
        return;
    }

    if(handle.eglMakeCurrent(g_EglDisplay, bundle->surface, bundle->surface, bundle->context)) {
        currentBundle = (basic_render_window_t *)bundle;
    } else {
        NSLog(@"EGLBridge: eglMakeCurrent returned with error: 0x%x", handle.eglGetError());
    }
}

void gl_swap_buffers() {
    if (!currentBundle) return;
    if (!handle.eglSwapBuffers(g_EglDisplay, currentBundle->gl.surface)) {
        EGLint error = handle.eglGetError();
        NSLog(@"EGLBridge: eglSwapBuffers failed: 0x%x", error);
    }
}

void gl_swap_interval(int swapInterval) {
    handle.eglSwapInterval(g_EglDisplay, swapInterval);
}

void gl_terminate() {
    handle.eglMakeCurrent(g_EglDisplay, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    if (currentBundle) {
        handle.eglDestroySurface(g_EglDisplay, currentBundle->gl.surface);
        handle.eglDestroyContext(g_EglDisplay, currentBundle->gl.context);
    }
    handle.eglTerminate(g_EglDisplay);
    handle.eglReleaseThread();
    free(currentBundle);
    currentBundle = nil;
}

void set_gl_bridge_tbl() {
    br_init = gl_init;
    br_init_context = (br_init_context_t) gl_init_context;
    br_make_current = (br_make_current_t) gl_make_current;
    br_swap_buffers = gl_swap_buffers;
    br_swap_interval = gl_swap_interval;
    br_terminate = gl_terminate;
}
