# Hermes research baseline

Odys Agent Platform Foundation was informed by a local, repository-external
checkout of:

- repository: `https://github.com/NousResearch/hermes-agent`
- commit: `3f315e46fede84ed4e6c8cfdbd00a13618e68986`
- commit subject: `Merge pull request #96963 from kshitijk4poor/refactor/fast-lane-consolidation`
- license: MIT
- upstream copyright: Nous Research, 2025

The checkout lived at the sibling research path
`D:\桌面\LHAS_Day0_Docs_v2\hermes-agent-reference`; it is outside the Odys
repository and is not part of the product commit.

## Research scope

The review covered the agent loop, prompt construction, provider profiles,
tool registry/toolsets, skills, memory provider, session storage/search, MCP
stdio lifecycle, delegation, plugins, and context compression. The exact
source-to-target decisions are recorded in `HERMES_ARCHITECTURE_MAP.md`.

## Import policy

No Hermes source module was copied or modified. Odys uses small native
Protocols, registries, adapters, services, and repositories. The existing Odys
Control Plane remains authoritative. `THIRD_PARTY_NOTICES.md` preserves the
upstream identity and MIT terms even though the implementation is a clean
reimplementation/reference use.
