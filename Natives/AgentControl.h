#import <Foundation/Foundation.h>

/// Handles the deliberately narrow `amethyst://agent/v1/...` control protocol.
/// Requests are accepted only from the already-registered custom URL scheme and
/// results are written to POJAV_HOME/agent-responses for House Arrest polling.
void AgentControlHandleURL(NSURL *url);
