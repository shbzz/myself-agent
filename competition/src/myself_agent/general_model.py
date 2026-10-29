"""DeepSeek multimodal/thinking adapter; preserve provider reasoning across tool turns."""

import asyncio
import ipaddress
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr


class WorkbenchDeepSeek(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for original, outgoing in zip(messages, payload["messages"], strict=True):
            if outgoing["role"] == "assistant":
                outgoing["reasoning_content"] = (
                    original.additional_kwargs.get("reasoning_content", "") or ""
                )
        return payload


class WorkbenchCompatible(ChatOpenAI):
    output_parameter: str = Field(default="max_tokens", exclude=True)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.output_parameter == "max_tokens" and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload


@contextmanager
def general_model_session(settings, effort="low", snapshot=None, credential=None):
    profile = (
        snapshot["profile"]
        if snapshot
        else {"model_id": settings.model, "streaming": True, "reasoning_parameter": "deepseek"}
    )
    provider = (
        snapshot["provider"]
        if snapshot
        else {"base_url": settings.base_url, "protocol": "deepseek"}
    )
    host = urlsplit(provider["base_url"]).hostname
    local = host == "localhost" or bool(host and host.endswith(".local"))
    try:
        local = local or ipaddress.ip_address(host).is_private
    except ValueError:
        pass
    proxy = None if local else settings.proxy
    with httpx.Client(proxy=proxy, timeout=90, trust_env=False) as client:
        options = {}
        if profile["reasoning_parameter"] == "deepseek":
            options["thinking"] = {"type": "disabled" if effort == "off" else "enabled"}
            if effort != "off":
                options["reasoning_effort"] = effort
        model_class = (
            WorkbenchDeepSeek if provider["protocol"] == "deepseek" else WorkbenchCompatible
        )
        kwargs = {}
        if provider["protocol"] != "deepseek":
            kwargs["output_parameter"] = profile.get("output_token_parameter", "max_tokens")
        if (
            profile["reasoning_parameter"] == "reasoning_effort"
            and effort == "off"
            and profile.get("reasoning_off_value")
        ):
            kwargs["reasoning_effort"] = profile["reasoning_off_value"]
        if profile["reasoning_parameter"] == "reasoning_effort" and effort != "off":
            kwargs["reasoning_effort"] = effort
        model = model_class(
            model=profile["model_id"],
            api_key=credential if credential is not None else settings.api_key,
            base_url=provider["base_url"],
            http_client=client,
            timeout=90,
            max_retries=0,
            max_tokens=snapshot["policy"]["output_reserve_tokens"] if snapshot else 6000,
            streaming=profile["streaming"],
            extra_body=options,
            **kwargs,
        )
        try:
            yield model
        finally:
            asyncio.run(model.root_async_client.close())


def provider_credential(store, provider, settings):
    reference = provider.get("credential_ref")
    if reference == "builtin:deepseek":
        return settings.api_key
    if reference:
        return SecretStr((store.root / "credentials" / reference).read_text())
    return SecretStr("no-key-required")


def model_base_settings(config_path, provider):
    from myself_agent.config import Settings, load_settings

    if provider.get("credential_ref") == "builtin:deepseek":
        return load_settings(config_path)
    return Settings(api_key=SecretStr("no-key-required"))
