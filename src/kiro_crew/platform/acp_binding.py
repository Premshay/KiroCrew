"""Safe engine bindings for ACP construction sites outside provider factories."""

from __future__ import annotations

from typing import Any

from kiro_crew.platform.context import current_context
from kiro_crew.platform.defaults import DefaultProviderRegistry
from kiro_crew.platform.interfaces import ACP_CLIENT_BINDING_KEYS


def runtime_client_binding(agent_name: str) -> dict[str, Any]:
    """Return a validated binding for a direct ``AcpRuntime`` construction.

    The binding describes which engine serves an agent. Callers retain their
    own sandbox, work directory, MCP gateway, and audit settings.
    """
    providers = current_context().providers
    if isinstance(providers, DefaultProviderRegistry):
        return {}
    lookup = getattr(providers, "agent_client_binding", None)
    if not callable(lookup):
        return {}
    raw = lookup(agent_name)
    if not isinstance(raw, dict):
        return {}

    binding: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in ACP_CLIENT_BINDING_KEYS:
            continue
        if key == "extra_env":
            if isinstance(value, dict) and value:
                binding[key] = {str(name): str(item) for name, item in value.items()}
        elif isinstance(value, str) and value:
            binding[key] = value
    return binding


def apply_runtime_client_binding(runtime_kwargs: dict[str, Any], binding: dict[str, Any]) -> None:
    """Apply a binding without discarding pre-existing security settings."""
    extra_env = binding.get("extra_env")
    if isinstance(extra_env, dict):
        inherited_env = runtime_kwargs.get("extra_env")
        runtime_kwargs["extra_env"] = {
            **(inherited_env if isinstance(inherited_env, dict) else {}),
            **extra_env,
        }
    runtime_kwargs.update({key: value for key, value in binding.items() if key != "extra_env"})
