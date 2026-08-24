# Workspace and Safe CLI (E3)

E3 provides a provider-neutral `Workspace` abstraction backed by `LocalReadOnlyWorkspace`.
All paths are canonicalized and must remain below the workspace root, including symlink targets.
The exposed capabilities are read-only: `workspace.list`, `workspace.read`, and `workspace.search`.

`cli.exec` is policy-constrained subprocess execution, not a Docker, VM, or kernel sandbox, and Safe CLI is not a filesystem sandbox. “Read-only Workspace” means that Odys E3 exposes no write/edit/delete capability; a subprocess explicitly allowed by `CommandPolicy` still runs with host OS permissions and may have filesystem side effects. E3 must therefore not be used as strong isolation for an untrusted repository plus untrusted command. The caller injects explicit token-prefix `CommandRule` entries; the default policy denies all commands. Processes use `shell=False`, no stdin, a workspace-relative cwd, sanitized environment variables, bounded timeouts, and bounded stdout/stderr.

Successful process execution is an observation even when its exit code is nonzero (for example, a failing test). Infrastructure failures such as `COMMAND_NOT_ALLOWED`, `WORKSPACE_PATH_ESCAPE`, `COMMAND_TIMEOUT`, and `SPAWN_ERROR` are ToolResult failures.

Architecture:

```text
Inner Agent -> Capability -> ToolRegistry
                         |-> workspace.list
                         |-> workspace.search
                         |-> workspace.read
                         `-> cli.exec -> LocalReadOnlyWorkspace
```

E3 deliberately does not provide write, delete, rename, git mutation, Skills, MCP, Memory, Browser, or dynamic replanning.
