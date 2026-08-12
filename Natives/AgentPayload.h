#import <Foundation/Foundation.h>

/// Resolve a verified file from an active host-staged payload.
/// Returns nil with no error when no payload with `payloadName` is active.
/// Returns nil with an error when an active pointer exists but fails its manifest contract.
NSString *AgentPayloadResolveActiveFile(NSString *payloadName, NSString *relativePath, NSError **error);

/// Return either a verified hot-payload file or the bundled @rpath library path.
/// Invalid active payloads fail closed instead of silently falling back.
NSString *AgentPayloadLibraryPath(NSString *bundledLibraryName, NSString *payloadName, NSError **error);
