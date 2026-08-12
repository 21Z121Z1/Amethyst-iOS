# Runtime contracts

Use a versioned runtime contract to catch packaging/classpath prerequisites before a physical launch. The verifier is intentionally generic; each AgentDebug test profile should keep its own contract next to the payload it validates.

Example:

```json
{
  "version": 1,
  "files": [
    {"path": "runtime/lib/example.dylib", "sha256": "<expected sha256>"}
  ],
  "archives": [
    {
      "path": "agents/spvc-classpath-agent.jar",
      "required_entries": [
        "META-INF/MANIFEST.MF",
        "example/SpvcClasspathAgent.class",
        "example/SpvcClasspathAgent$1.class"
      ]
    }
  ]
}
```

Paths are relative to the manifest directory and may not escape it. Run `python3 -m tools.amethystd.runtime_contract` only through a wrapper or call `verify_contract()` from an agent workflow; the repository tests exercise the same implementation.
