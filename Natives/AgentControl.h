#import <Foundation/Foundation.h>

/// Starts the durable Documents inbox used by `amethyst-agent/v2`.
/// Safe to call more than once; the poller is process-lifetime scoped.
void AgentControlStart(void);

/// Handles the backwards-compatible `amethyst://agent/v1/...` URL protocol.
void AgentControlHandleURL(NSURL *url);

/// Emits an append-only lifecycle event for the active v2 launch run, if any.
/// Subsystems such as the JVM launcher, renderer bridge and test-only Minecraft probe
/// can use this without knowing how the host transport works.
void AgentControlEmitActiveEvent(NSString *event, NSDictionary *payload);
