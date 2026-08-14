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
#include <string.h>
#include <sys/types.h>

#include "EGL/egl.h"
#include "EGL/eglext.h"
#include "GL/osmesa.h"

#include "glfw_keycodes.h"
#include "ctxbridges/bridge_tbl.h"
#include "ctxbridges/osmesa_internal.h"
#include "utils.h"

int clientAPI;

static NSString *RendererCanonicalPath(NSString *path) {
    if (!path.length) return nil;
    return [[[NSURL fileURLWithPath:path] URLByResolvingSymlinksInPath].path stringByStandardizingPath];
}

static NSString *RendererImagePath(void *address) {
    if (!address) return nil;
    Dl_info info;
    memset(&info, 0, sizeof(info));
    if (dladdr(address, &info) == 0 || !info.dli_fname) return nil;
    return RendererCanonicalPath(@(info.dli_fname));
}

static NSDictionary *RendererProvenance(void *rendererHandle, NSString *rendererPath) {
    BOOL hot = [rendererPath hasPrefix:@"/"];
    NSString *expected = hot ? RendererCanonicalPath(rendererPath) : nil;
    NSArray<NSString *> *symbols = @[ @"eglGetProcAddress", @"eglMakeCurrent", @"glGetString", @"glGetIntegerv", @"glDrawElements" ];
    NSMutableDictionary *images = [NSMutableDictionary dictionary];
    BOOL ok = YES;
    for (NSString *symbol in symbols) {
        void *address = dlsym(RTLD_DEFAULT, symbol.UTF8String);
        NSString *image = RendererImagePath(address);
        images[symbol] = image ?: @"<missing>";
        if (hot && (!image || ![image isEqualToString:expected])) ok = NO;
    }
    NSString *loadedImage = RendererImagePath(dlsym(rendererHandle, "glGetString"));
    if (hot && (!loadedImage || ![loadedImage isEqualToString:expected])) ok = NO;
    NSString *digest = hot ? rendererPath.stringByDeletingLastPathComponent.lastPathComponent.lowercaseString : @"bundled";
    return @{
        @"provenance_ok": @(ok),
        @"requested_library_path": rendererPath ?: @"<unset>",
        @"loaded_library_path": loadedImage ?: @"<unknown>",
        @"candidate_digest": digest ?: @"<unknown>",
        @"symbol_images": images,
    };
}

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

    // A hot payload must be the same image LWJGL opens. Passing only the basename
    // can load the bundled Frameworks copy and create two independent GL states.
    NSString *lwjglLibrary = [rendererPath hasPrefix:@"/"] ? rendererPath : renderer;
    JNI_LWJGL_changeRenderer(lwjglLibrary.UTF8String);

    void *rendererHandle = dlopen(rendererPath.UTF8String, RTLD_NOW | RTLD_GLOBAL);
    if (!rendererHandle) {
        const char *loadError = dlerror();
        BOOL codeSignatureFailure = loadError && strstr(loadError, "code signature") != NULL;
        NSLog(@"EGLBridge: failed to preload renderer %@: %s", renderer, loadError ?: "unknown error");
        AgentControlEmitActiveEvent(@"failed", @{
            @"failure_class": codeSignatureFailure ? @"DYLD_VALIDATION_FAILURE" : @"NATIVE_DYLIB_LOAD_FAILURE",
            @"reason": codeSignatureFailure ? @"renderer_dlopen_code_signature_failed" : @"renderer_dlopen_failed",
            @"renderer": renderer ?: @"<unset>",
            @"hot_payload": @([rendererPath hasPrefix:@"/"]),
            @"dlerror": @(loadError ?: "unknown error")
        });
        return 1;
    }

    NSDictionary *provenance = RendererProvenance(rendererHandle, rendererPath);
    if ([rendererPath hasPrefix:@"/"] && ![provenance[@"provenance_ok"] boolValue]) {
        NSLog(@"EGLBridge: hot payload provenance mismatch: %@", provenance);
        NSMutableDictionary *failure = [provenance mutableCopy];
        failure[@"failure_class"] = @"HOT_PAYLOAD_PROVENANCE_MISMATCH";
        failure[@"reason"] = @"renderer_symbols_resolved_to_different_image";
        failure[@"renderer"] = renderer ?: @"<unset>";
        failure[@"hot_payload"] = @YES;
        AgentControlEmitActiveEvent(@"failed", failure);
        dlclose(rendererHandle);
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

    NSMutableDictionary *ready = [provenance mutableCopy];
    ready[@"renderer"] = renderer ?: @"<unset>";
    ready[@"hot_payload"] = @([rendererPath hasPrefix:@"/"]);
    AgentControlEmitActiveEvent(@"renderer_ready", ready);
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
