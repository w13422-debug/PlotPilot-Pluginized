"""Browser-only deterministic provider seam for the P0 acceptance harness.

The product's existing mock provider is intentionally kept untouched: the
integration worktree must not edit donor-owned business implementation.  This
module is imported only by ``browser_smoke.mjs`` and adds the one missing
contract-shaped response needed to drive the existing Autopilot UI through act
planning and into writing.  The daemon wrapper preserves the product's normal
multiprocessing injection of stream, shared-state, and persistence queues.
"""
from __future__ import annotations

import json
import re
import asyncio
from typing import Any


_ORIGINAL_DAEMON_PROCESS: Any = None
_ORIGINAL_STREAM_GENERATE: Any = None
_INSTALLED = False


def _act_plan_chapter_count(prompt: Any) -> int:
    """Read the already-rendered contract count without changing its meaning."""
    text = "\n".join(
        str(getattr(prompt, name, "") or "") for name in ("system", "user")
    )
    patterns = (
        r"请为这一幕规划\s*(\d+)\s*个章节",
        r"chapter_count\D{0,24}(\d+)",
        r"章节数量\D{0,24}(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))
    # The rendered contract requires a positive count. This fallback is only
    # for an unexpected prompt renderer and remains deterministic.
    return 1


def _act_plan_payload(prompt: Any) -> str:
    count = _act_plan_chapter_count(prompt)
    chapters = []
    for number in range(1, count + 1):
        previous = "本幕入口" if number == 1 else f"第{number - 1}章留下的未完因果"
        next_handoff = (
            "把本幕主线交给下一幕"
            if number == count
            else f"把第{number}章的后果交给第{number + 1}章"
        )
        chapters.append(
            {
                "number": number,
                "title": f"第{number}章 · 验收",
                "main_event": f"沿着当前幕的已授权冲突推进第{number}个主事件。",
                "handoff_from_previous": previous,
                "handoff_to_next": next_handoff,
                "required_threads": ["当前幕核心因果"],
                "location_hint": "当前幕核心地点",
                "cast_hint": ["核心人物甲"],
                "characters": ["核心人物甲"],
                "locations": ["location_starting_point"],
                "thrill_type": "hook" if number == 1 else "action",
                "thrill_description": "以可验证的选择和后果推进主线。",
                "foreshadow_action": "plant_and_resolve" if number == count else "plant",
                "foreshadow_detail": "为后续因果保留可回收线索。",
            }
        )
    return json.dumps({"chapters": chapters}, ensure_ascii=False, separators=(",", ":"))


def install_browser_smoke_provider() -> None:
    """Install the acceptance-only act-plan extension exactly once per process."""
    global _INSTALLED, _ORIGINAL_STREAM_GENERATE
    if _INSTALLED:
        return

    # Import lazily so the seam does not alter the product import graph before
    # the backend has initialized its normal runtime.
    from infrastructure.ai.providers.mock_provider import MockProvider, MockResponseFactory

    original_build = MockResponseFactory.build

    def build_with_act_plan(self, prompt):
        text = "\n".join(
            str(getattr(prompt, name, "") or "") for name in ("system", "user")
        ).lower()
        if "planning-act" in text or "请为这一幕规划" in text:
            return _act_plan_payload(prompt)
        return original_build(self, prompt)

    MockResponseFactory.build = build_with_act_plan

    # The donor MockProvider deliberately emits deterministic chunks as fast as
    # Python can iterate them.  That is correct for unit tests but makes a
    # browser stop action race the terminal event: the real UI removes its
    # 「停止」 button as soon as the stream completes.  Keep the provider and
    # product untouched; in this acceptance-only seam, pace only chapter prose
    # streams so Playwright can perform an actual visible cancel action.  The
    # delay is after each yielded chunk, so AbortController cancellation still
    # travels through the real UI → API → workflow chain.
    _ORIGINAL_STREAM_GENERATE = MockProvider.stream_generate

    async def stream_with_browser_timing(self, prompt, config):
        # PromptContract rendering intentionally does not expose its node key
        # in the user-visible prompt, so this seam must not depend on a marker
        # that may be stripped by a renderer.  The smoke runs only with this
        # isolated provider; pacing every deterministic stream keeps both
        # wizard and chapter actions observable without changing their data.
        pace_chapter_stream = True
        async for chunk in _ORIGINAL_STREAM_GENERATE(self, prompt, config):
            yield chunk
            if pace_chapter_stream:
                await asyncio.sleep(0.20)

    MockProvider.stream_generate = stream_with_browser_timing
    _INSTALLED = True


def run_autopilot_daemon_process_with_browser_provider(
    stop_event,
    log_level: int,
    log_file: str,
    stream_queue=None,
    shared_state=None,
    persistence_queue=None,
) -> None:
    """Run the normal daemon after installing the P0-only provider seam.

    ``_ORIGINAL_DAEMON_PROCESS`` handles forked test runners; on Windows the
    child imports this module afresh and captures the donor implementation
    before delegating. In both cases the original queue/shared-state wiring is
    used verbatim.
    """
    global _ORIGINAL_DAEMON_PROCESS
    install_browser_smoke_provider()
    if _ORIGINAL_DAEMON_PROCESS is None:
        from interfaces.daemon_manager import run_autopilot_daemon_process

        _ORIGINAL_DAEMON_PROCESS = run_autopilot_daemon_process
    return _ORIGINAL_DAEMON_PROCESS(
        stop_event,
        log_level,
        log_file,
        stream_queue,
        shared_state,
        persistence_queue,
    )


def patch_daemon_manager_for_browser_smoke() -> None:
    """Make the standard manager use the acceptance wrapper, not a second daemon."""
    global _ORIGINAL_DAEMON_PROCESS
    import interfaces.daemon_manager as daemon_manager

    if _ORIGINAL_DAEMON_PROCESS is None:
        _ORIGINAL_DAEMON_PROCESS = daemon_manager.run_autopilot_daemon_process
    daemon_manager.run_autopilot_daemon_process = (
        run_autopilot_daemon_process_with_browser_provider
    )
