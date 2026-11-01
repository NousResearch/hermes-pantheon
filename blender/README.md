<p align="center"><img src="../assets/blender.jpg" alt="Blender: official Hermes Agent plugin" width="100%"></p>

# Blender integration for Hermes

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-plugin-blender`](https://github.com/NousResearch/hermes-plugin-blender), which is archived; this directory carries its full history.

A portable Hermes plugin containing MCP launch configuration and a workflow skill for the [official Blender Lab MCP server](https://www.blender.org/lab/mcp-server/).

## Install

```sh
hermes plugins install blender
hermes plugins enable blender
```

This installs plugin 0.1.0 from the Hermes plugin catalog. Installing straight from this repository (`hermes plugins install NousResearch/hermes-plugin-blender`) installs 0.2.0, which needs a Hermes release that includes NousResearch/hermes-agent#134050 (location lists in `app:` declarations); released Hermes up to the current release refuses it with `app.win32.location is required` on every OS.

Choose the destination profile with the global `hermes -p <profile>` option.

## Prerequisites

- Blender 5.1 or newer on the same machine as the Hermes backend. If none is found, the skill offers to install the newest official release from download.blender.org into your own folders (no administrator rights), checked against blender.org's sha256 list. Blender 5.1+ has no build for Intel Macs or Linux on ARM.
- The official Blender Lab MCP add-on in Blender. The skill offers to install it (the pinned v1.0.3 extension, through Blender's own command line), in the same question when it also installs Blender, or walks you through the official page.
- Hermes opens Blender itself when a task needs it, with `--online-mode` so the add-on can start its bridge on loopback port 9876 without changing your saved preferences. A Blender you open yourself needs **Allow Online Access** turned on for the bridge to start.
- Git and uv available to the backend. Network access is required for the initial MCP environment setup.
- Optional, for the `*_for_cli` tools that open a saved `.blend` file in a separate background Blender: a `blender` command on the backend's `PATH`. `BLENDER_PATH` set in a shell or `.env` does not reach the server.

See [setup and troubleshooting](skills/blender/references/setup.md). Installing this package does **not** install Blender, change Blender preferences, install its add-on, or open a scene; the skill opens Blender when a task needs it, and installs Blender or the add-on only after you agree.

## Prepare the server before first activation

The first upstream download/build took several minutes in our Windows checks. Warm its isolated uv environment before asking Hermes to connect, so a normal MCP connection timeout does not interrupt the download:

```sh
uv tool run --from "git+https://projects.blender.org/lab/blender_mcp.git@2cea8d566dde07fbac28a61d698909d69724e853#subdirectory=mcp" blender-mcp --help
```

Restart Hermes, or use the explicit plugin/MCP reload commands supported by your release. The package's skill is available through `skills_list` and its qualified name via `skill_view`. Verify a real `get_objects_summary` response before treating Blender as ready.

## What runs

Hermes launches the official server over stdio with the same pinned source as the preparation command. The MCP server connects to the Blender add-on over `127.0.0.1:9876`. No native Hermes Python hooks, tool overrides, desktop JavaScript or self-updater are included.

The upstream revision is fixed to `2cea8d566dde07fbac28a61d698909d69724e853` (v1.0.3). uv resolves its Python dependencies in an isolated tool environment; those transitive dependencies are not fully locked by this package.

## Security and licensing

Blender Lab warns that its tools execute arbitrary Python in Blender without protection against file deletion or data exfiltration. Use only with trusted workflows, keep the bridge loopback-only, and prefer a disposable scene or isolated machine for experiments. This is not a sandbox.

This uses Blender Lab's source on `projects.blender.org`, not the unrelated PyPI package with the same `blender-mcp` name. Do not substitute an unqualified package install.

The wrapper configuration and original documentation are MIT-licensed. The external Blender Lab server and add-on remain GPL-3.0-or-later, owned by their respective authors. No Blender Lab code is vendored here.

## Verified scope

See [verification](VERIFICATION.md). Headless scene read/create/transform/readback/delete passed on Windows ARM64 and x64 with Blender 5.2.2 LTS. Graphical screenshots, deferred operations and liveness detection (whether Blender and its bridge are running) are not verified or implemented by this wrapper. MCP tool listing alone does not establish scene readiness.
