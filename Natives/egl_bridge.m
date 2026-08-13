#import "SurfaceViewController.h"
#import "AgentControl.h"
#import "AgentPayload.h"

#include "jni.h"
#include <assert.h>
#include <dlfcn.h>

#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>

#include "EGL/egl.h"
#include "EGL/eglext.h"
#include "GL/osmesa.h"

#include "glfw_keycodes.h"
#include "ctxbridges/bridge_tbl.h"
#include "ctxbridges/osmesa_internal.h"
#include "utils.h"

int clientAPI;

void JNI_LWJGL_changeRenderer(const char* value_c) {
    JNIEnv *env;
    (*runtimeJavaVMPtr)->GetEnv(runtimeJavaVMPtr, (void **)&env, JNI_VERSION_1_4);
    jstring key = (*env)->NewStringUTF(env, "org.lwjgl.opengl.libname");
    jstring value = (*env)->NewStringUTF(env, value_c);
    jclass clazz = (*env)->FindClass(env, "java/lang/System");
    jmethodID method = (*env)->GetStaticMethodID(env, clazz, "setProperty", "(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;");
    (*env)->CallStaticObjectMethod(env, clazz, method, key, value);
}

void pojavTerminate() {
    CallbackBridge_nativeSetInputReady(NO);
    if (!br_terminate) return;
    br_terminate();
}

void* pojavGetCurrentContext() {
    return br_get_current();
}

int pojavInit(BOOL useStackQueue) {
    clientAPI = GLFW_OPENGL_API;
    isInputReady = 1;
    isUseStackQueueCall = useStackQueue;
    return JNI_TRUE;
}

int pojavInitOpenGL() {
    NSString *renderer = NSProcessInfo.processInfo.environment[@"POJAV_RENDERER"];
    AgentControlEmitActiveEvent(@"renderer_loading", @{
        @"renderer": renderer ?: @"<unset>"
    });

    BOOL isAuto = [renderer isEqualToString:@"auto"];
    if (isAuto || [renderer isEqualToString:@ RENDERER_NAME_GL4ES]) {
        renderer = @ RENDERER_NAME_GL4ES;
        setenv("POJAV_RENDERER", renderer.UTF8String, 1);
        set_gl_bridge_tbl();
    } else if ([renderer isEqualToString:@ RENDERER_NAME_MOBILEGLUES]) {
        renderer = @ RENDERER_NAME_MOBILEGLUES;
        setenv("POJAV_RENDERER", renderer.UTF8String, 1);
        set_gl_bridge_tbl();
    } else if ([renderer isEqualToString:@ RENDERER_NAME_MTL_ANGLE]) {
        set_gl_bridge_tbl();
    } else if ([renderer isEqualToString:@ RENDERER_NAME_MITHRIL]) {
        set_gl_bridge_tbl();
    } else if ([renderer hasPrefix:@"libOSMesa"]) {
        setenv("GALLIUM_DRIVER","zink",1);
        set_osm_bridge_tbl();
    }

    if (!br_init) {
        NSLog(@"EGLBridge: no bridge initializer for renderer=%@", renderer ?: @"<unset>");
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": @"RENDERER_INIT_FAILURE",
            @"reason": @"bridge_initializer_unavailable",
            @"renderer": renderer ?: @"<unset>"
        });
        return 1;
    }

    JNI_LWJGL_changeRenderer(renderer.UTF8String);
    NSError *payloadError = nil;
    NSString *payloadName = [renderer isEqualToString:@ RENDERER_NAME_MITHRIL] ? @"mithril" : nil;
    NSString *rendererPath = AgentPayloadLibraryPath(renderer, payloadName, &payloadError);
    if (!rendererPath) {
        NSLog(@"EGLBridge: active renderer payload rejected: %@", payloadError.localizedDescription);
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": @"RENDERER_INIT_FAILURE",
            @"reason": @"hot_payload_verification_failed",
            @"renderer": renderer ?: @"<unset>"
        });
        return 1;
    }

    void *rendererHandle = dlopen(rendererPath.UTF8String, RTLD_NOW | RTLD_GLOBAL);
    if (!rendererHandle) {
        NSLog(@"EGLBridge: failed to preload renderer %@: %s", renderer, dlerror() ?: "unknown error");
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": @"NATIVE_DYLIB_LOAD_FAILURE",
            @"reason": @"renderer_dlopen_failed",
            @"renderer": renderer ?: @"<unset>",
            @"hot_payload": @([rendererPath hasPrefix:@"/"])
        });
        return 1;
    }

    if (!br_init()) {
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": @"RENDERER_INIT_FAILURE",
            @"reason": @"egl_bridge_init_failed",
            @"renderer": renderer ?: @"<unset>",
            @"hot_payload": @([rendererPath hasPrefix:@"/"])
        });
        return 1;
    }

    AgentControlEmitActiveEvent(@"renderer_ready", @{
        @"renderer": renderer ?: @"<unset>",
        @"hot_payload": @([rendererPath hasPrefix:@"/"])
    });
    return 0;
}

void pojavSetWindowHint(int hint, int value) {
    if (hint == GLFW_CLIENT_API) {
        clientAPI = value;
    } else if (strcmp(getenv("POJAV_RENDERER"), "auto")==0 && hint == GLFW_CONTEXT_VERSION_MAJOR) {
        switch (value) {
            case 1:
            case 2:
                setenv("POJAV_RENDERER", RENDERER_NAME_GL4ES, 1);
                JNI_LWJGL_changeRenderer(RENDERER_NAME_GL4ES);
                break;
            default:
                setenv("POJAV_RENDERER", RENDERER_NAME_MOBILEGLUES, 1);
                JNI_LWJGL_changeRenderer(RENDERER_NAME_MOBILEGLUES);
                break;
        }
    }
}

void pojavSwapBuffers() {
    br_swap_buffers();
}

void pojavMakeCurrent(basic_render_window_t* window) {
    br_make_current(window);
}

void* pojavCreateContext(basic_render_window_t* contextSrc) {
    if (clientAPI == GLFW_NO_API) {
        return (__bridge void *)SurfaceViewController.surface.layer;
    }

    static BOOL inited = NO;
    if (!inited) {
        inited = YES;
        if (pojavInitOpenGL() != 0) {
            NSLog(@"EGLBridge: renderer initialization failed");
            return NULL;
        }
    }

    if (!br_init_context) {
        NSLog(@"EGLBridge: renderer bridge context callback is unavailable (renderer=%@)",
              NSProcessInfo.processInfo.environment[@"POJAV_RENDERER"] ?: @"<unset>");
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": @"RENDERER_INIT_FAILURE",
            @"reason": @"bridge_context_callback_unavailable"
        });
        return NULL;
    }
    void *context = br_init_context(contextSrc);
    if (!context) {
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": @"RENDERER_INIT_FAILURE",
            @"reason": @"context_creation_failed"
        });
    }
    return context;
}

void pojavSwapInterval(int interval) {
    if (!br_swap_interval) return;
    br_swap_interval(interval);
}
