"""dispatch: bring your own sandbox to Hermes Agent.

    hermes plugins install dispatch
    hermes config set plugins.entries.dispatch.settings.program ~/.hermes/dispatch.sh
    hermes config set terminal.backend dispatch

A program you write turns any infrastructure (Docker, Kubernetes, Apple containers, E2B, a VM pool)
into a Hermes terminal backend: Hermes calls it to spawn, exec in, pause and delete a sandbox per
session. The contract is in ``dispatch_env.py`` and README.md.
"""

from __future__ import annotations

from .provider import DispatchProvider


def register(ctx) -> None:
    ctx.register_terminal_environment_provider(DispatchProvider(get_config=ctx.get_config))


__all__ = ["DispatchProvider", "register"]
