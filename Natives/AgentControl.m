#import "AgentControl.h"

#import "LauncherNavigationController.h"
#import "LauncherPreferences.h"
#import "LauncherSplitViewController.h"
#import "PLProfiles.h"
#import "SurfaceViewController.h"
#import "ios_uikit_bridge.h"
#import "utils.h"

#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "glfw_keycodes.h"

static dispatch_source_t AgentInboxTimer;
static NSString *AgentActiveRunID;
static uint64_t AgentEventSequence;

static NSString *AgentSafeIdentifier(NSString *value) {
    if (value.length == 0 || value.length > 96) return nil;
    NSCharacterSet *allowed = [NSCharacterSet characterSetWithCharactersInString:
        @"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"];
    for (NSUInteger i = 0; i < value.length; i++) {
        if (![allowed characterIsMember:[value characterAtIndex:i]]) return nil;
    }
    return value;
}

static NSString *AgentRequestID(NSString *requestID) {
    return AgentSafeIdentifier(requestID) ?: NSUUID.UUID.UUIDString;
}

static NSString *AgentHomePath(void) {
    const char *home = getenv("POJAV_HOME");
    return home ? @(home) : nil;
}

static NSString *AgentProcessGeneration(void) {
    static NSString *generation;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        generation = [NSString stringWithFormat:@"%d-%.0f-%@", getpid(),
            [NSDate.date timeIntervalSince1970] * 1000.0, NSUUID.UUID.UUIDString];
    });
    return generation;
}

static dispatch_queue_t AgentEventQueue(void) {
    static dispatch_queue_t queue;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        queue = dispatch_queue_create("org.angelauramc.amethyst.agent-events", DISPATCH_QUEUE_SERIAL);
    });
    return queue;
}

static NSString *AgentDirectory(NSString *name) {
    NSString *home = AgentHomePath();
    return home.length ? [home stringByAppendingPathComponent:name] : nil;
}

static BOOL AgentEnsureDirectory(NSString *name) {
    NSString *path = AgentDirectory(name);
    if (!path.length) return NO;
    NSError *error = nil;
    BOOL ok = [NSFileManager.defaultManager createDirectoryAtPath:path
                                      withIntermediateDirectories:YES
                                                       attributes:nil
                                                            error:&error];
    if (!ok) NSLog(@"[AgentControl] create %@ failed: %@", name, error.localizedDescription);
    return ok;
}

static NSString *AgentResponsePath(NSString *requestID) {
    return [AgentDirectory(@"agent-responses")
        stringByAppendingPathComponent:[AgentRequestID(requestID) stringByAppendingString:@".json"]];
}

static BOOL AgentWriteJSON(NSDictionary *object, NSString *path) {
    if (!path.length) return NO;
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:object options:NSJSONWritingPrettyPrinted error:&error];
    if (!data || ![data writeToFile:path options:NSDataWritingAtomic error:&error]) {
        NSLog(@"[AgentControl] JSON write failed: %@", error.localizedDescription);
        return NO;
    }
    return YES;
}

static void AgentWriteResponse(NSString *protocol, NSString *requestID, NSString *runID, NSDictionary *payload) {
    if (!AgentEnsureDirectory(@"agent-responses")) return;
    NSMutableDictionary *response = [payload mutableCopy] ?: [NSMutableDictionary dictionary];
    response[@"protocol"] = protocol;
    response[@"request_id"] = AgentRequestID(requestID);
    response[@"timestamp"] = @([NSDate.date timeIntervalSince1970]);
    response[@"process_id"] = @(getpid());
    response[@"process_generation"] = AgentProcessGeneration();
    if ([protocol isEqualToString:@"amethyst-agent/v2"]) response[@"run_id"] = AgentSafeIdentifier(runID) ?: @"invalid";
    AgentWriteJSON(response, AgentResponsePath(requestID));
}

static void AgentWriteResponseV1(NSString *requestID, NSDictionary *payload) {
    AgentWriteResponse(@"amethyst-agent/v1", requestID, nil, payload);
}

static void AgentWriteResponseV2(NSString *requestID, NSString *runID, NSDictionary *payload) {
    AgentWriteResponse(@"amethyst-agent/v2", requestID, runID, payload);
}

static void AgentWriteEvent(NSString *runID, NSString *event, NSDictionary *payload) {
    NSString *safeRunID = AgentSafeIdentifier(runID);
    if (!safeRunID.length || !event.length) return;
    dispatch_sync(AgentEventQueue(), ^{
        if (!AgentEnsureDirectory(@"agent-events")) return;
        NSMutableDictionary *record = [payload mutableCopy] ?: [NSMutableDictionary dictionary];
        record[@"protocol"] = @"amethyst-agent/v2";
        record[@"event"] = event;
        record[@"event_id"] = NSUUID.UUID.UUIDString;
        record[@"run_id"] = safeRunID;
        record[@"seq"] = @(++AgentEventSequence);
        record[@"timestamp"] = @([NSDate.date timeIntervalSince1970]);
        record[@"process_id"] = @(getpid());
        record[@"process_generation"] = AgentProcessGeneration();

        NSError *error = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:record options:0 error:&error];
        if (!json) {
            NSLog(@"[AgentControl] event serialization failed: %@", error.localizedDescription);
            return;
        }
        NSMutableData *line = [json mutableCopy];
        [line appendBytes:"\n" length:1];
        NSString *path = [AgentDirectory(@"agent-events")
            stringByAppendingPathComponent:[safeRunID stringByAppendingString:@".jsonl"]];
        if (![NSFileManager.defaultManager fileExistsAtPath:path]) {
            [[NSData data] writeToFile:path atomically:YES];
        }
        NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:path];
        if (!handle) return;
        @try {
            [handle seekToEndOfFile];
            [handle writeData:line];
            [handle synchronizeFile];
        } @catch (NSException *exception) {
            NSLog(@"[AgentControl] event append failed: %@", exception.reason);
        } @finally {
            [handle closeFile];
        }
    });
}

void AgentControlEmitActiveEvent(NSString *event, NSDictionary *payload) {
    NSString *runID;
    @synchronized (NSProcessInfo.processInfo) {
        runID = [AgentActiveRunID copy];
    }
    if (runID.length) AgentWriteEvent(runID, event, payload ?: @{});
}

static void AgentSetActiveRunID(NSString *runID) {
    @synchronized (NSProcessInfo.processInfo) {
        AgentActiveRunID = [AgentSafeIdentifier(runID) copy];
    }
}

static NSDictionary<NSString *, NSString *> *AgentQueryItems(NSURLComponents *components) {
    NSMutableDictionary *items = [NSMutableDictionary dictionary];
    for (NSURLQueryItem *item in components.queryItems ?: @[]) {
        if (item.name.length && item.value.length) items[item.name] = item.value;
    }
    return items;
}

static BOOL AgentBoolValue(id value, BOOL defaultValue) {
    if (!value || value == NSNull.null) return defaultValue;
    if ([value isKindOfClass:NSNumber.class]) return [value boolValue];
    if (![value isKindOfClass:NSString.class] || ![value length]) return defaultValue;
    return [@[@"1", @"true", @"yes", @"on"] containsObject:[value lowercaseString]];
}

static NSString *AgentRebaseContainerPath(NSString *value) {
    if (![value isKindOfClass:NSString.class] || !value.length) return value;
    NSString *home = AgentHomePath();
    if (!home.length) return value;

    NSString *containerRoot = [home hasSuffix:@"/Documents"]
        ? [home substringToIndex:home.length - @"/Documents".length]
        : home;

    NSRegularExpression *expression = [NSRegularExpression
        regularExpressionWithPattern:@"/private/var/mobile/Containers/Data/Application/[0-9A-Fa-f-]+(?:/Documents)+"
        options:0 error:nil];
    return [expression stringByReplacingMatchesInString:value
        options:0 range:NSMakeRange(0, value.length)
        withTemplate:[containerRoot stringByAppendingString:@"/Documents"]];
}

static void AgentRebaseProfilePaths(NSMutableDictionary *profile) {
    if (![profile isKindOfClass:NSMutableDictionary.class]) return;
    NSString *javaArgs = profile[@"javaArgs"];
    if ([javaArgs isKindOfClass:NSString.class]) {
        profile[@"javaArgs"] = AgentRebaseContainerPath(javaArgs);
    }
}

static BOOL AgentSetGameDirectory(NSString *instance, NSString **errorMessage) {
    if (!instance.length || [instance isEqualToString:@"."] || [instance isEqualToString:@".."] ||
        [instance containsString:@"/"] || [instance containsString:@"\\"] || [instance containsString:@".."]) {
        if (errorMessage) *errorMessage = @"invalid instance name";
        return NO;
    }
    NSString *home = AgentHomePath();
    if (!home.length) {
        if (errorMessage) *errorMessage = @"POJAV_HOME is unavailable";
        return NO;
    }

    NSFileManager *fm = NSFileManager.defaultManager;
    NSString *instancePath = [home stringByAppendingPathComponent:[NSString stringWithFormat:@"instances/%@", instance]];
    BOOL isDirectory = NO;
    if (![fm fileExistsAtPath:instancePath isDirectory:&isDirectory] || !isDirectory) {
        if (errorMessage) *errorMessage = @"requested instance does not exist";
        return NO;
    }
    if (SurfaceViewController.isRunning) {
        if (errorMessage) *errorMessage = @"cannot change instance while Minecraft is running";
        return NO;
    }

    NSString *gamePath = [home stringByAppendingPathComponent:@"Library/Application Support/minecraft"];
    struct stat gameStat;
    BOOL gamePathExists = lstat(gamePath.fileSystemRepresentation, &gameStat) == 0;
    if (gamePathExists && !S_ISLNK(gameStat.st_mode)) {
        if (errorMessage) *errorMessage = @"refusing to replace a non-symlink game directory";
        return NO;
    }
    NSString *temporaryLink = [gamePath stringByAppendingFormat:@".agent-%@", NSUUID.UUID.UUIDString];
    if (symlink(instancePath.fileSystemRepresentation, temporaryLink.fileSystemRepresentation) != 0) {
        if (errorMessage) *errorMessage = [NSString stringWithFormat:@"failed to create instance link: %s", strerror(errno)];
        return NO;
    }
    if (rename(temporaryLink.fileSystemRepresentation, gamePath.fileSystemRepresentation) != 0) {
        unlink(temporaryLink.fileSystemRepresentation);
        if (errorMessage) *errorMessage = [NSString stringWithFormat:@"failed to activate instance link: %s", strerror(errno)];
        return NO;
    }
    if (![fm changeCurrentDirectoryPath:gamePath]) {
        if (errorMessage) *errorMessage = @"failed to change current directory";
        return NO;
    }

    setPrefObject(@"general.game_directory", instance);
    setenv("POJAV_GAME_DIR", gamePath.UTF8String, 1);
    [PLProfiles updateCurrent];
    return YES;
}

static LauncherNavigationController *AgentLauncherNavigationController(void) {
    UIViewController *root = UIWindow.mainWindow.rootViewController;
    if (![root isKindOfClass:LauncherSplitViewController.class]) return nil;
    for (UIViewController *controller in ((UISplitViewController *)root).viewControllers) {
        if ([controller isKindOfClass:LauncherNavigationController.class]) return (LauncherNavigationController *)controller;
    }
    return nil;
}

static NSDictionary *AgentStatus(void) {
    NSMutableDictionary *profile = [NSMutableDictionary dictionary];
    PLProfiles *profiles = PLProfiles.current;
    NSMutableDictionary *selected = profiles.selectedProfile;
    if (profiles.selectedProfileName) profile[@"name"] = profiles.selectedProfileName;
    if (selected[@"lastVersionId"]) profile[@"version"] = selected[@"lastVersionId"];
    if (selected[@"renderer"]) profile[@"renderer"] = [PLProfiles resolveKeyForCurrentProfile:@"renderer"];
    if ([selected[@"quickPlaySingleplayer"] isKindOfClass:NSString.class])
        profile[@"quick_play_singleplayer"] = selected[@"quickPlaySingleplayer"];
    NSString *instance = getPrefObject(@"general.game_directory");
    if (instance) profile[@"instance"] = instance;
    return @{
        @"ok": @YES,
        @"state": SurfaceViewController.isRunning ? @"game_running" : @"launcher",
        @"profile": profile,
        @"process_id": @(getpid()),
        @"process_generation": AgentProcessGeneration(),
    };
}

static NSDictionary *AgentRuntimeProbe(void) {
    NSMutableDictionary *result = [AgentStatus() mutableCopy];
    result[@"probe"] = @{
        @"kind": @"runtime",
        @"pojav_home_available": @([AgentHomePath() length] > 0),
        @"game_surface_running": @(SurfaceViewController.isRunning),
    };
    return result;
}

static NSDictionary *AgentJITProbe(void) {
    JITFlags flags = DeviceGetJITFlags(YES);
    NSMutableDictionary *result = [AgentStatus() mutableCopy];
    result[@"probe"] = @{
        @"kind": @"jit",
        @"cs_debugged": @(isJITEnabled(YES)),
        @"flags": @((NSUInteger)flags),
        @"ios26": @((flags & JIT_FLAG_IS_IOS_26) != 0),
        @"force_mirrored": @((flags & JIT_FLAG_FORCE_MIRRORED) != 0),
        @"has_txm": @((flags & JIT_FLAG_HAS_TXM) != 0),
        @"likely_debugger_keep_attached": @(JIT26IsLikelyDebuggerKeepAttached()),
        @"universal_mapping_verified": @NO,
        @"universal_mapping_verification_owner": @"host-supervisor",
    };
    return result;
}

static NSNumber *AgentNumber(NSDictionary *params, NSString *key) {
    id value = params[key];
    if ([value isKindOfClass:NSNumber.class]) return value;
    if ([value isKindOfClass:NSString.class]) {
        NSScanner *scanner = [NSScanner scannerWithString:value];
        NSInteger parsed = 0;
        if ([scanner scanInteger:&parsed] && scanner.isAtEnd) return @(parsed);
    }
    return nil;
}

static NSDictionary *AgentInputResult(NSString *state) {
    NSMutableDictionary *result = [AgentStatus() mutableCopy];
    result[@"state"] = state;
    result[@"input"] = @YES;
    return result;
}

static NSDictionary *AgentHandleAction(NSString *action, NSDictionary *params) {
    if ([action isEqualToString:@"status"]) return AgentStatus();
    if ([action isEqualToString:@"probe/runtime"]) return AgentRuntimeProbe();
    if ([action isEqualToString:@"probe/jit"]) return AgentJITProbe();

    if ([action isEqualToString:@"input/key"]) {
        if (!SurfaceViewController.isRunning)
            return @{ @"ok": @NO, @"state": @"input_rejected", @"error": @"Minecraft is not running" };
        NSNumber *keyValue = AgentNumber(params, @"key");
        NSNumber *actionValue = AgentNumber(params, @"action");
        NSNumber *scancodeValue = AgentNumber(params, @"scancode");
        NSNumber *modsValue = AgentNumber(params, @"mods");
        if (!keyValue || !actionValue || keyValue.intValue < 0 ||
            keyValue.intValue > GLFW_KEY_LAST ||
            (actionValue.intValue != 0 && actionValue.intValue != 1) ||
            (modsValue && (modsValue.intValue < 0 || modsValue.intValue > 255))) {
            return @{ @"ok": @NO, @"state": @"input_rejected", @"error": @"invalid key input" };
        }
        CallbackBridge_nativeSendKey(
            keyValue.intValue,
            scancodeValue ? scancodeValue.intValue : 0,
            actionValue.intValue,
            modsValue ? modsValue.intValue : 0);
        return AgentInputResult(@"input_key_sent");
    }

    if ([action isEqualToString:@"input/mouse"]) {
        if (!SurfaceViewController.isRunning)
            return @{ @"ok": @NO, @"state": @"input_rejected", @"error": @"Minecraft is not running" };
        NSNumber *buttonValue = AgentNumber(params, @"button");
        NSNumber *actionValue = AgentNumber(params, @"action");
        NSNumber *modsValue = AgentNumber(params, @"mods");
        if (!buttonValue || !actionValue || buttonValue.intValue < 0 ||
            buttonValue.intValue > 7 ||
            (actionValue.intValue != 0 && actionValue.intValue != 1) ||
            (modsValue && (modsValue.intValue < 0 || modsValue.intValue > 255))) {
            return @{ @"ok": @NO, @"state": @"input_rejected", @"error": @"invalid mouse input" };
        }
        CallbackBridge_nativeSendMouseButton(
            buttonValue.intValue,
            actionValue.intValue,
            modsValue ? modsValue.intValue : 0);
        return AgentInputResult(@"input_mouse_sent");
    }

    if ([action isEqualToString:@"terminate"]) {
        BOOL force = AgentBoolValue(params[@"force"], NO);
        if (!SurfaceViewController.isRunning) return @{ @"ok": @YES, @"state": @"already_in_launcher" };
        NSDictionary *response = @{ @"ok": @YES,
            @"state": force ? @"app_exit_requested" : @"return_to_launcher_requested" };
        if (force) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 150 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ exit(0); });
        } else {
            UIKit_returnToSplitView();
        }
        return response;
    }

    if (![action isEqualToString:@"profile/set"] && ![action isEqualToString:@"launch"]) {
        return @{ @"ok": @NO, @"state": @"rejected", @"error": @"unsupported agent action" };
    }
    if (SurfaceViewController.isRunning) {
        return @{ @"ok": @NO, @"state": @"blocked_game_running", @"error": @"stop Minecraft before changing profile" };
    }

    NSString *failure = nil;
    NSString *instance = [params[@"instance"] isKindOfClass:NSString.class] ? params[@"instance"] : nil;
    if (instance.length && !AgentSetGameDirectory(instance, &failure)) {
        return @{ @"ok": @NO, @"state": @"profile_change_failed", @"error": failure ?: @"unknown error" };
    }

    NSString *profileName = [params[@"profile"] isKindOfClass:NSString.class] ? params[@"profile"] : nil;
    NSString *version = [params[@"version"] isKindOfClass:NSString.class] ? params[@"version"] : nil;
    PLProfiles *profiles = PLProfiles.current;
    if (!profileName.length) profileName = profiles.selectedProfileName;
    NSMutableDictionary *profile = profiles.profiles[profileName];
    if (!profile) return @{ @"ok": @NO, @"state": @"profile_not_found", @"error": @"requested profile does not exist" };
    for (NSMutableDictionary *candidate in profiles.profiles.allValues) {
        AgentRebaseProfilePaths(candidate);
    }
    NSString *globalJavaArgs = getPrefObject(@"java.java_args");
    if ([globalJavaArgs isKindOfClass:NSString.class]) {
        setPrefObject(@"java.java_args", AgentRebaseContainerPath(globalJavaArgs));
    }
    if (version.length) profile[@"lastVersionId"] = version;
    NSString *quickPlay = [params[@"quickPlaySingleplayer"] isKindOfClass:NSString.class]
        ? params[@"quickPlaySingleplayer"] : params[@"quick_play_singleplayer"];
    if ([quickPlay isKindOfClass:NSString.class]) {
        if (quickPlay.length) profile[@"quickPlaySingleplayer"] = quickPlay;
        else [profile removeObjectForKey:@"quickPlaySingleplayer"];
    }
    profiles.selectedProfileName = profileName;
    [profiles save];

    LauncherNavigationController *launcher = AgentLauncherNavigationController();
    [launcher reloadProfileList];
    if ([action isEqualToString:@"profile/set"]) return AgentStatus();
    if (!launcher) return @{ @"ok": @NO, @"state": @"launcher_not_ready", @"error": @"launcher navigation controller is unavailable" };
    if (![launcher agentLaunchWithError:&failure]) {
        return @{ @"ok": @NO, @"state": @"launch_rejected", @"error": failure ?: @"launch rejected" };
    }
    NSMutableDictionary *response = [AgentStatus() mutableCopy];
    response[@"state"] = @"launch_requested";
    return response;
}

static void AgentProcessV2Envelope(NSDictionary *request, NSString *claimedRequestID) {
    NSString *protocol = [request[@"protocol"] isKindOfClass:NSString.class] ? request[@"protocol"] : nil;
    NSString *requestID = AgentSafeIdentifier([request[@"request_id"] isKindOfClass:NSString.class] ? request[@"request_id"] : nil);
    NSString *runID = AgentSafeIdentifier([request[@"run_id"] isKindOfClass:NSString.class] ? request[@"run_id"] : nil);
    NSString *action = [request[@"action"] isKindOfClass:NSString.class] ? request[@"action"] : nil;
    NSDictionary *params = [request[@"params"] isKindOfClass:NSDictionary.class] ? request[@"params"] : @{};
    if (![protocol isEqualToString:@"amethyst-agent/v2"] || !requestID.length || !runID.length ||
        !action.length || ![requestID isEqualToString:claimedRequestID]) {
        AgentWriteResponseV2(claimedRequestID, runID ?: @"invalid", @{
            @"ok": @NO, @"state": @"rejected", @"error": @"invalid v2 request envelope"
        });
        return;
    }

    NSString *guard = [request[@"if_process_generation"] isKindOfClass:NSString.class] ? request[@"if_process_generation"] : nil;
    if (guard.length && ![guard isEqualToString:AgentProcessGeneration()]) {
        AgentWriteResponseV2(requestID, runID, @{
            @"ok": @NO, @"state": @"stale_process_generation", @"error": @"process generation changed"
        });
        return;
    }

    AgentWriteEvent(runID, @"command_received", @{ @"action": action, @"request_id": requestID });
    if ([action isEqualToString:@"launch"]) AgentSetActiveRunID(runID);
    NSDictionary *result = AgentHandleAction(action, params);
    if ([action isEqualToString:@"launch"] && ![result[@"ok"] boolValue]) AgentSetActiveRunID(nil);
    AgentWriteResponseV2(requestID, runID, result);
    AgentWriteEvent(runID, @"command_completed", @{
        @"action": action,
        @"request_id": requestID,
        @"ok": @([result[@"ok"] boolValue]),
        @"state": result[@"state"] ?: @"unknown",
    });
    if ([action isEqualToString:@"launch"] && [result[@"ok"] boolValue]) {
        AgentWriteEvent(runID, @"launch_accepted", @{ @"profile": result[@"profile"] ?: @{} });
    }
}

static BOOL AgentMoveReplacing(NSString *source, NSString *target) {
    if (!source.length || !target.length) return NO;
    NSFileManager *fm = NSFileManager.defaultManager;
    [fm removeItemAtPath:target error:nil];
    NSError *error = nil;
    BOOL ok = [fm moveItemAtPath:source toPath:target error:&error];
    if (!ok) NSLog(@"[AgentControl] move %@ -> %@ failed: %@", source, target, error.localizedDescription);
    return ok;
}

static void AgentPollInbox(void) {
    if (!AgentEnsureDirectory(@"agent-requests")) return;
    NSString *requestDir = AgentDirectory(@"agent-requests");
    NSString *processingDir = AgentDirectory(@"agent-processing");
    NSString *processedDir = AgentDirectory(@"agent-processed");
    NSArray<NSString *> *files = [[NSFileManager.defaultManager contentsOfDirectoryAtPath:requestDir error:nil]
        sortedArrayUsingSelector:@selector(compare:)];
    NSUInteger count = 0;
    for (NSString *file in files) {
        if (count >= 8 || ![file hasSuffix:@".json"]) continue;
        NSString *requestID = [file stringByDeletingPathExtension];
        if (![(AgentSafeIdentifier(requestID) ?: @"") isEqualToString:requestID]) continue;
        NSString *source = [requestDir stringByAppendingPathComponent:file];
        NSString *claimed = [processingDir stringByAppendingPathComponent:file];
        NSString *done = [processedDir stringByAppendingPathComponent:file];
        if ([NSFileManager.defaultManager fileExistsAtPath:AgentResponsePath(requestID)]) {
            AgentMoveReplacing(source, done);
            count++;
            continue;
        }
        if (!AgentMoveReplacing(source, claimed)) continue;
        NSData *data = [NSData dataWithContentsOfFile:claimed];
        NSError *error = nil;
        id object = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:&error] : nil;
        if (![object isKindOfClass:NSDictionary.class]) {
            AgentWriteResponseV2(requestID, @"invalid", @{
                @"ok": @NO, @"state": @"rejected", @"error": error.localizedDescription ?: @"invalid JSON request"
            });
        } else {
            AgentProcessV2Envelope((NSDictionary *)object, requestID);
        }
        AgentMoveReplacing(claimed, done);
        count++;
    }
}

static void AgentRecoverProcessing(void) {
    for (NSString *directory in @[@"agent-requests", @"agent-responses", @"agent-events", @"agent-processing", @"agent-processed"]) {
        AgentEnsureDirectory(directory);
    }
    NSString *processingDir = AgentDirectory(@"agent-processing");
    for (NSString *file in [NSFileManager.defaultManager contentsOfDirectoryAtPath:processingDir error:nil] ?: @[]) {
        if (![file hasSuffix:@".json"]) continue;
        NSString *requestID = [file stringByDeletingPathExtension];
        NSString *source = [processingDir stringByAppendingPathComponent:file];
        NSString *targetDir = [NSFileManager.defaultManager fileExistsAtPath:AgentResponsePath(requestID)]
            ? AgentDirectory(@"agent-processed") : AgentDirectory(@"agent-requests");
        AgentMoveReplacing(source, [targetDir stringByAppendingPathComponent:file]);
    }
}

void AgentControlStart(void) {
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        AgentRecoverProcessing();
        AgentInboxTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
        dispatch_source_set_timer(AgentInboxTimer,
            dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC),
            250 * NSEC_PER_MSEC,
            50 * NSEC_PER_MSEC);
        dispatch_source_set_event_handler(AgentInboxTimer, ^{ AgentPollInbox(); });
        dispatch_resume(AgentInboxTimer);
        NSLog(@"[AgentControl] v2 inbox started for process generation %@", AgentProcessGeneration());
    });
}

static void AgentProcessURL(NSURL *url) {
    NSURLComponents *components = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
    NSDictionary *query = AgentQueryItems(components);
    NSString *requestID = AgentRequestID(query[@"request_id"]);
    if (![components.scheme.lowercaseString isEqualToString:@"amethyst"] ||
        ![components.host.lowercaseString isEqualToString:@"agent"] ||
        ![components.path hasPrefix:@"/v1/"]) {
        AgentWriteResponseV1(requestID, @{
            @"ok": @NO, @"state": @"rejected", @"error": @"unsupported agent URL"
        });
        return;
    }
    NSString *action = [components.path substringFromIndex:@"/v1/".length];
    NSMutableDictionary *params = [NSMutableDictionary dictionary];
    for (NSString *key in @[@"profile", @"version", @"instance"]) {
        if (query[key]) params[key] = query[key];
    }
    if (query[@"force"]) params[@"force"] = @(AgentBoolValue(query[@"force"], NO));
    AgentWriteResponseV1(requestID, AgentHandleAction(action, params));
}

void AgentControlHandleURL(NSURL *url) {
    if (!url) return;
    dispatch_async(dispatch_get_main_queue(), ^{ AgentProcessURL(url); });
}
