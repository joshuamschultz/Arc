"""Build and run a bounded browser-use agent for ``browser_task``.

This is a bounded internal executor: a single browser-use agent, capped
at ``max_steps``, with its LLM routed through arcllm. It is not a second
ArcRun loop — it is one tool that happens to iterate internally, the same
shape as any complex tool. Importing this module pulls in the optional
``browser_use`` package, so it is only imported at call time.
"""

from __future__ import annotations

# browser_use is imported at module top ON PURPOSE: importing this module is
# what fails when the optional extra is absent, so browser_task's caller
# catches that ImportError and degrades loudly. Do not defer these imports.
from arcllm import load_model
from browser_use import Agent
from browser_use.browser import BrowserProfile, BrowserSession

from arcagent.modules.browser._browser_use.adapter import ArcLLMChatModel
from arcagent.modules.browser.config import BrowserUseConfig


async def run_browser_task(goal: str, cfg: BrowserUseConfig) -> str:
    """Run a browser-use agent to accomplish ``goal``; return its result."""
    provider = load_model(cfg.llm_provider, cfg.llm_model or None)
    llm = ArcLLMChatModel(provider, model=provider.model_name)

    session = None
    if cfg.cdp_url:
        session = BrowserSession(browser_profile=BrowserProfile(cdp_url=cfg.cdp_url))

    agent = Agent(
        task=goal,
        llm=llm,
        browser_session=session,
        use_vision=cfg.use_vision,
        max_actions_per_step=cfg.max_actions_per_step,
        step_timeout=cfg.step_timeout_s,
    )
    try:
        history = await agent.run(max_steps=cfg.max_steps)
    finally:
        if session is not None:
            await session.kill()
        await provider.close()

    result = history.final_result()
    return result if result else str(history)
