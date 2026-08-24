# 32 - Environment Failure Stop

When a required development environment component is unavailable or fails, the agent MUST STOP and report the problem instead of improvising a workaround.

This includes failures involving:

- Python interpreters or virtual environments.
- Package imports.
- MCP servers or MCP packages.
- Node/npm environments.
- Test runners.
- Build tools.
- External services required by the current task.
- Credentials or configuration required for execution.

The agent MUST NOT, without explicit user authorization:

- Install or upgrade dependencies.
- Create mock, fake, stub, replacement, or simulated packages.
- Create temporary modules that imitate unavailable infrastructure.
- Modify environment configuration to bypass the failure.
- Change PATH or interpreter selection permanently.
- Modify tests or snapshots to hide the failure.
- Replace a real integration with a simulated implementation.
- Continue implementation based on assumptions about the missing component.

Required behavior:

1. Identify the exact failure.
2. Verify whether the required component already exists elsewhere in the project environment.
3. If an existing valid component is found, report the correct execution method before proceeding.
4. If the component is genuinely unavailable, STOP.
5. Report the failure, its evidence, and the minimum action required from the user.
6. Wait for explicit authorization before making environmental changes.

A development task MUST NOT be considered successful merely because a workaround or simulation makes the code executable.

Real infrastructure must be tested against real infrastructure.

Environment failures are blocking conditions, not implementation problems.