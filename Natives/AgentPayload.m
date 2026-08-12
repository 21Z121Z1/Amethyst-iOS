#import "AgentPayload.h"

#import <CommonCrypto/CommonDigest.h>

#include <stdlib.h>

static NSString *const AgentPayloadErrorDomain = @"org.angelauramc.amethyst.agent-payload";

typedef NS_ENUM(NSInteger, AgentPayloadErrorCode) {
    AgentPayloadErrorInvalidContract = 1,
    AgentPayloadErrorVerificationFailed = 2,
};

static void AgentPayloadSetError(NSError **error, AgentPayloadErrorCode code, NSString *message) {
    if (!error) return;
    *error = [NSError errorWithDomain:AgentPayloadErrorDomain
                                 code:code
                             userInfo:@{NSLocalizedDescriptionKey: message ?: @"payload verification failed"}];
}

static NSString *AgentPayloadHome(void) {
    const char *home = getenv("POJAV_HOME");
    return home ? @(home) : nil;
}

static BOOL AgentPayloadSafeIdentifier(NSString *value) {
    if (value.length == 0 || value.length > 96) return NO;
    NSCharacterSet *allowed = [NSCharacterSet characterSetWithCharactersInString:
        @"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"];
    for (NSUInteger i = 0; i < value.length; i++) {
        if (![allowed characterIsMember:[value characterAtIndex:i]]) return NO;
    }
    return YES;
}

static BOOL AgentPayloadSafeRelativePath(NSString *value) {
    if (value.length == 0 || [value hasPrefix:@"/"] || [value containsString:@"\\"]) return NO;
    for (NSString *component in [value componentsSeparatedByString:@"/"]) {
        if (component.length == 0 || [component isEqualToString:@"."] || [component isEqualToString:@".."]) return NO;
    }
    return YES;
}

static BOOL AgentPayloadValidDigest(NSString *value) {
    if (value.length != 64) return NO;
    NSCharacterSet *hex = [NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdefABCDEF"];
    return [[value stringByTrimmingCharactersInSet:hex] length] == 0;
}

static NSDictionary *AgentPayloadReadDictionary(NSString *path, NSError **error) {
    NSData *data = [NSData dataWithContentsOfFile:path options:0 error:error];
    if (!data) return nil;
    id value = [NSJSONSerialization JSONObjectWithData:data options:0 error:error];
    return [value isKindOfClass:NSDictionary.class] ? value : nil;
}

static NSString *AgentPayloadSHA256(NSString *path, NSError **error) {
    NSInputStream *stream = [NSInputStream inputStreamWithFileAtPath:path];
    [stream open];
    if (stream.streamStatus == NSStreamStatusError) {
        if (error) *error = stream.streamError;
        return nil;
    }

    CC_SHA256_CTX context;
    CC_SHA256_Init(&context);
    uint8_t buffer[1024 * 1024];
    NSInteger count;
    while ((count = [stream read:buffer maxLength:sizeof(buffer)]) > 0) {
        CC_SHA256_Update(&context, buffer, (CC_LONG)count);
    }
    if (count < 0) {
        if (error) *error = stream.streamError;
        [stream close];
        return nil;
    }
    [stream close];

    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &context);
    NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (NSUInteger i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", digest[i]];
    return hex;
}

NSString *AgentPayloadResolveActiveFile(NSString *payloadName, NSString *relativePath, NSError **error) {
    if (error) *error = nil;
    if (!AgentPayloadSafeIdentifier(payloadName) || !AgentPayloadSafeRelativePath(relativePath)) {
        AgentPayloadSetError(error, AgentPayloadErrorInvalidContract, @"invalid payload name or relative path");
        return nil;
    }

    NSString *home = AgentPayloadHome();
    if (home.length == 0) {
        AgentPayloadSetError(error, AgentPayloadErrorInvalidContract, @"POJAV_HOME is unavailable");
        return nil;
    }

    NSString *activeRoot = [home stringByAppendingPathComponent:@"agent-payloads/active"];
    NSString *pointerPath = [activeRoot stringByAppendingPathComponent:[payloadName stringByAppendingString:@".json"]];
    if (![NSFileManager.defaultManager fileExistsAtPath:pointerPath]) return nil;

    NSError *localError = nil;
    NSDictionary *pointer = AgentPayloadReadDictionary(pointerPath, &localError);
    NSString *pointerName = [pointer[@"name"] isKindOfClass:NSString.class] ? pointer[@"name"] : nil;
    NSString *digest = [pointer[@"digest"] isKindOfClass:NSString.class] ? pointer[@"digest"] : nil;
    NSString *stage = [pointer[@"stage"] isKindOfClass:NSString.class] ? pointer[@"stage"] : nil;
    NSString *expectedStage = AgentPayloadValidDigest(digest)
        ? [NSString stringWithFormat:@"agent-payloads/.staging/%@", digest.lowercaseString] : nil;
    if (!pointer || ![pointerName isEqualToString:payloadName] || !AgentPayloadValidDigest(digest) ||
        (stage.length > 0 && ![stage.lowercaseString isEqualToString:expectedStage])) {
        AgentPayloadSetError(error, AgentPayloadErrorInvalidContract,
            localError.localizedDescription ?: @"active payload pointer failed validation");
        return nil;
    }

    NSString *stageRoot = [home stringByAppendingPathComponent:expectedStage];
    NSString *manifestPath = [stageRoot stringByAppendingPathComponent:@"manifest.json"];
    NSDictionary *manifest = AgentPayloadReadDictionary(manifestPath, &localError);
    NSString *manifestName = [manifest[@"name"] isKindOfClass:NSString.class] ? manifest[@"name"] : nil;
    NSString *manifestDigest = [manifest[@"digest"] isKindOfClass:NSString.class] ? manifest[@"digest"] : nil;
    NSArray *files = [manifest[@"files"] isKindOfClass:NSArray.class] ? manifest[@"files"] : nil;
    if (!manifest || ![manifest[@"version"] isEqual:@1] || ![manifestName isEqualToString:payloadName] ||
        ![manifestDigest.lowercaseString isEqualToString:digest.lowercaseString] || !files) {
        AgentPayloadSetError(error, AgentPayloadErrorInvalidContract,
            localError.localizedDescription ?: @"payload manifest failed validation");
        return nil;
    }

    NSDictionary *entry = nil;
    for (id candidate in files) {
        if (![candidate isKindOfClass:NSDictionary.class]) continue;
        NSString *path = [candidate[@"path"] isKindOfClass:NSString.class] ? candidate[@"path"] : nil;
        if ([path isEqualToString:relativePath]) {
            entry = candidate;
            break;
        }
    }
    NSNumber *expectedSize = [entry[@"size"] isKindOfClass:NSNumber.class] ? entry[@"size"] : nil;
    NSString *expectedSHA = [entry[@"sha256"] isKindOfClass:NSString.class] ? entry[@"sha256"] : nil;
    if (!entry || !expectedSize || !AgentPayloadValidDigest(expectedSHA)) {
        AgentPayloadSetError(error, AgentPayloadErrorInvalidContract, @"requested file is absent from payload manifest");
        return nil;
    }

    NSString *resolved = [[stageRoot stringByAppendingPathComponent:relativePath] stringByStandardizingPath];
    NSString *standardStage = [stageRoot stringByStandardizingPath];
    if (![resolved hasPrefix:[standardStage stringByAppendingString:@"/"]]) {
        AgentPayloadSetError(error, AgentPayloadErrorInvalidContract, @"resolved payload path escaped staging root");
        return nil;
    }

    NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:resolved error:&localError];
    if (!attributes || ![attributes[NSFileType] isEqual:NSFileTypeRegular] ||
        ![attributes[NSFileSize] isEqualToNumber:expectedSize]) {
        AgentPayloadSetError(error, AgentPayloadErrorVerificationFailed,
            localError.localizedDescription ?: @"payload file size/type verification failed");
        return nil;
    }

    NSString *actualSHA = AgentPayloadSHA256(resolved, &localError);
    if (!actualSHA || ![actualSHA.lowercaseString isEqualToString:expectedSHA.lowercaseString]) {
        AgentPayloadSetError(error, AgentPayloadErrorVerificationFailed,
            localError.localizedDescription ?: @"payload SHA-256 verification failed");
        return nil;
    }
    return resolved;
}

NSString *AgentPayloadLibraryPath(NSString *bundledLibraryName, NSString *payloadName, NSError **error) {
    if (error) *error = nil;
    if (payloadName.length > 0) {
        NSString *hot = AgentPayloadResolveActiveFile(payloadName, bundledLibraryName, error);
        if (hot || (error && *error)) return hot;
    }
    return [NSString stringWithFormat:@"@rpath/%@", bundledLibraryName];
}
