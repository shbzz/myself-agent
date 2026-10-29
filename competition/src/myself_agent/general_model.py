"""DeepSeek multimodal/thinking adapter; preserve provider reasoning across tool turns."""

import asyncio
from contextlib import contextmanager

import httpx
from langchain_deepseek import ChatDeepSeek


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


@contextmanager
def general_model_session(settings, effort="low"):
    with httpx.Client(proxy=settings.proxy, timeout=90, trust_env=False) as client:
        options = {"thinking": {"type": "disabled" if effort == "off" else "enabled"}}
        if effort != "off":
            options["reasoning_effort"] = effort
        model = WorkbenchDeepSeek(
            model=settings.model,
            api_key=settings.api_key,
            api_base=settings.base_url,
            http_client=client,
            timeout=90,
            max_retries=0,
            max_tokens=6000,
            streaming=True,
            extra_body=options,
        )
        try:
            yield model
        finally:
            asyncio.run(model.root_async_client.close())
