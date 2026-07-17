"""sb3.agents — Phase 1 lifecycle placeholders.

These prove the launchd contract `sb3-ctl kill` depends on (bootstrap → run
under KeepAlive → clean SIGTERM exit). They do no SDR work and hold no leases.
Phase 2+ replaces the bodies; the lifecycle contract stays.
"""

__all__ = ["broker_stub", "controller_stub"]
