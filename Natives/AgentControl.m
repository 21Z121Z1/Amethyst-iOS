#import "AgentControl.h"

#import "LauncherNavigationController.h"
#import "LauncherPreferences.h"
#import "LauncherSplitViewController.h"
#import "PLProfiles.h"
#import "SurfaceViewController.h"
#import "ios_uikit_bridge.h"
#import "utils.h"

#include <stdlib.h>
#include <errno.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static NSString *AgentSafeRequestID(NSString *requestID) {
    if (requestID.length == 0) {
        requestID = NSUUID.UUID.UUIDString;
    }
    NSMutableString *safe = [NSMutableString string];
    NSCharacterSet *allowed = [NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"];
    for (NSUInteger i = 0; i < requestID.length && safe.length < 96; i++) {
        unichar c = [requestID characterAtIndex:i];
        [safe appendFormat:@"%c", [allowed characterIsMember:c] ? c : '_'];
    }
    return safe.length > 0 ? safe : NSUUID.UUID.UUIDString;
}

static NSString *AgentHomePath(void) {
    const char *home = getenv("POJAV_HOME");
    return home ? @(home) : nil;
}

static void AgentWriteResponse(NSString *requestID, NSDictionary *payload) {
    NSString *home = AgentHomePath();
    if (home.length == 0) {
        NSLog(@"[AgentControl] POJAV_HOME is unavailable; response was not persisted");
        return;
    }

    NSString *responseDir = [home stringByAppendingPathComponent:@"agent-responses"];
    [NSFileManager.defaultManager createDirectoryAtPath:responseDir
                             withIntermediateDirectories:YES
                                              attributes:nil
                                                   error:nil];
    NSMutableDictionary *response = [payload mutableCopy] ?: [NSMutableDictionary dictionary];
    response[@"protocol"] = @"amethyst-agent/v1";
    response[@"request_id"] = AgentSafeRequestID(requestID);
    response[@"timestamp"] = @([[NSDate date] timeIntervalSince1970]);

    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:response options:NSJSONWritingPrettyPrinted error:&error];
    if (!data || ![data writeToFile:[responseDir stringByAppendingPathComponent:[AgentSafeRequestID(requestID) stringByAppendingString:@".json"]]
                           options:NSDataWritingAtomic
                             error:&error]) {
        NSLog(@"[AgentControl] response write failed: %@", error.localizedDescription);
    }
}

static NSDictionary<NSString *, NSString *> *AgentQueryItems(NSURLComponents *components) {
    NSMutableDictionary *items = [NSMutableDictionary dictionary];
    for (NSURLQueryItem *item in components.queryItems ?: @[]) {
        if (item.name.length > 0 && item.value.length > 0) {
            items[item.name] = item.value;
        }
    }
    return items;
}

static BOOL AgentBool(NSString *value, BOOL defaultValue) {
    if (value.length == 0) return defaultValue;
    return [@[@"1", @"true", @"yes", @"on"] containsObject:value.lowercaseString];
}

static BOOL AgentSetGameDirectory(NSString *instance, NSString **errorMessage) {
    if (instance.length == 0 || [instance isEqualToString:@"."] || [instance isEqualToString:@".."] ||
        [instance containsString:@"/"] || [instance containsString:@"\\"] || [instance containsString:@".."] ) {
        if (errorMessage) *errorMessage = @"invalid instance name";
        return NO;
    }

    NSString *home = AgentHomePath();
    if (home.length == 0) {
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
        if ([controller isKindOfClass:LauncherNavigationController.class]) {
            return (LauncherNavigationController *)controller;
        }
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
    NSString *instance = getPrefObject(@"general.game_directory");
    if (instance) profile[@"instance"] = instance;

    return @{
        @"ok": @YES,
        @"state": SurfaceViewController.isRunning ? @"game_running" : @"launcher",
        @"profile": profile,
        @"process_id": @(getpid()),
    };
}

static void AgentProcessURL(NSURL *url) {
    NSURLComponents *components = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
    NSString *requestID = AgentSafeRequestID([AgentQueryItems(components)[@"request_id"] copy]);
    NSString *failure = nil;

    if (![components.scheme.lowercaseString isEqualToString:@"amethyst"] ||
        ![components.host.lowercaseString isEqualToString:@"agent"] ||
        ![components.path hasPrefix:@"/v1/"]) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"rejected", @"error": @"unsupported agent URL" });
        return;
    }

    NSDictionary *query = AgentQueryItems(components);
    NSString *action = [components.path substringFromIndex:@"/v1/".length];
    if ([action isEqualToString:@"status"]) {
        AgentWriteResponse(requestID, AgentStatus());
        return;
    }

    if ([action isEqualToString:@"terminate"]) {
        BOOL force = AgentBool(query[@"force"], NO);
        if (!SurfaceViewController.isRunning) {
            AgentWriteResponse(requestID, @{ @"ok": @YES, @"state": @"already_in_launcher" });
            return;
        }
        AgentWriteResponse(requestID, @{ @"ok": @YES, @"state": force ? @"app_exit_requested" : @"return_to_launcher_requested" });
        if (force) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 150 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
                exit(0);
            });
        } else {
            UIKit_returnToSplitView();
        }
        return;
    }

    if (![action isEqualToString:@"profile/set"] && ![action isEqualToString:@"launch"]) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"rejected", @"error": @"unsupported agent action" });
        return;
    }
    if (SurfaceViewController.isRunning) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"blocked_game_running", @"error": @"stop Minecraft before changing profile" });
        return;
    }

    NSString *instance = query[@"instance"];
    if (instance.length > 0 && !AgentSetGameDirectory(instance, &failure)) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"profile_change_failed", @"error": failure ?: @"unknown error" });
        return;
    }

    NSString *profileName = query[@"profile"];
    NSString *version = query[@"version"];
    PLProfiles *profiles = PLProfiles.current;
    if (profileName.length == 0) profileName = profiles.selectedProfileName;
    NSMutableDictionary *profile = profiles.profiles[profileName];
    if (!profile) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"profile_not_found", @"error": @"requested profile does not exist" });
        return;
    }
    if (version.length > 0) profile[@"lastVersionId"] = version;
    profiles.selectedProfileName = profileName;
    [profiles save];

    LauncherNavigationController *launcher = AgentLauncherNavigationController();
    [launcher reloadProfileList];
    if ([action isEqualToString:@"profile/set"]) {
        AgentWriteResponse(requestID, AgentStatus());
        return;
    }
    if (!launcher) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"launcher_not_ready", @"error": @"launcher navigation controller is unavailable" });
        return;
    }
    if (![launcher agentLaunchWithError:&failure]) {
        AgentWriteResponse(requestID, @{ @"ok": @NO, @"state": @"launch_rejected", @"error": failure ?: @"launch rejected" });
        return;
    }
    NSMutableDictionary *launchResponse = [AgentStatus() mutableCopy];
    launchResponse[@"state"] = @"launch_requested";
    AgentWriteResponse(requestID, launchResponse);
}

void AgentControlHandleURL(NSURL *url) {
    if (!url) return;
    dispatch_async(dispatch_get_main_queue(), ^{
        AgentProcessURL(url);
    });
}
