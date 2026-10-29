import asyncio
from contextlib import contextmanager

import httpx
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_model_call
from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_deepseek import ChatDeepSeek

from myself_agent.config import Settings


@wrap_model_call
def platform_owned_delegation(request, handler):
    """Only the outer platform dispatches agents; disable nested task delegation."""
    tools = [
        tool
        for tool in request.tools
        if (tool.get("name") if isinstance(tool, dict) else tool.name) != "task"
    ]
    return handler(request.override(tools=tools))


@contextmanager
def model_session(settings: Settings):
    # Explicit proxy; credentials are used only by these trusted HTTP clients.
    with httpx.Client(proxy=settings.proxy, timeout=60, trust_env=False) as sync_client:
        model = ChatDeepSeek(
            model=settings.model,
            api_key=settings.api_key,
            api_base=settings.base_url,
            http_client=sync_client,
            timeout=60,
            max_retries=0,
            max_tokens=2400,
            extra_body={"thinking": {"type": "disabled"}},
        )
        try:
            yield model
        finally:
            # ChatDeepSeek also creates an async client, even on the sync-only path.
            asyncio.run(model.root_async_client.close())


def invoke_role(model, role: str, schema, prompt: str):
    usage = UsageMetadataCallbackHandler()
    agent = create_deep_agent(
        model=model,
        name=role,
        system_prompt=(
            f"You are the platform's {role}. Complete only the assigned bounded task. "
            "Return the required structured response immediately. Do not delegate, run code, "
            "install packages, use network tools, or inspect real host files. "
            "The platform independently validates, saves, and tests your response."
        ),
        subagents=[],
        middleware=[
            platform_owned_delegation,
            ModelCallLimitMiddleware(run_limit=2, exit_behavior="error"),
        ],
        response_format=ToolStrategy(schema, handle_errors=True),
        checkpointer=False,
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        {"recursion_limit": 12, "callbacks": [usage]},
        # This stateless child has no checkpointer; do not inherit the parent's
        # synchronous checkpoint barrier (unsupported without a child saver).
        durability="async",
    )
    response = result.get("structured_response")
    if response is None:
        raise ValueError("Agent did not produce a validated structured response")
    return schema.model_validate(response), usage.usage_metadata
