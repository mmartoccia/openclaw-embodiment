"""Optional LeRobot ActionQueue bridge.

If lerobot is installed, this replaces ActionChunkBuffer with the full
LeRobot RTC implementation (better blend scheduling, diffusion policy support,
Real-Time Control config).

If lerobot is NOT installed, falls back to our lightweight ActionChunkBuffer
which is API-compatible with LeRobot's ActionQueue.

Usage::

    from openclaw_embodiment.hal.lerobot_bridge import get_action_queue

    # Returns LeRobot ActionQueue when available, otherwise ActionChunkBuffer
    queue = get_action_queue(execution_horizon=10)

    # Push a chunk (same API for both implementations)
    queue.merge(chunk_commands)

    # Pop for control loop tick
    cmd = queue.get()

API compatibility matrix:
    Method              ActionChunkBuffer    LeRobot ActionQueue
    -------             -----------------    -------------------
    merge(chunk)        YES                  YES
    get()               YES                  YES
    get_left_over()     YES                  YES
    clear()             YES                  YES
    is_empty()          YES                  YES
    queue_depth         YES                  YES
    inference_lag_ms    YES                  NO (not exposed by LeRobot)

Note: LeRobot's ActionQueue uses RTCConfig for blend scheduling. The
`execution_horizon` parameter maps to RTCConfig.execution_horizon.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_action_queue(execution_horizon: int = 10, **kwargs):
    """Return an ActionQueue for decoupled LLM-to-control-loop buffering.

    Attempts to use LeRobot's RTC ActionQueue implementation. Falls back
    to our lightweight ActionChunkBuffer when lerobot is not installed.

    Args:
        execution_horizon: Number of overlap/blend steps at chunk boundaries.
                           LeRobot calls this the 'execution horizon'.
                           ActionChunkBuffer uses it as blend_steps.
        **kwargs: Additional keyword arguments passed to RTCConfig (lerobot only).
                  Silently ignored by ActionChunkBuffer.

    Returns:
        LeRobot ActionQueue if lerobot is installed, otherwise ActionChunkBuffer.

    Example::

        # Fast path -- always works, zero deps
        queue = get_action_queue(execution_horizon=10)
        queue.merge(my_commands)
        cmd = queue.get()

        # When lerobot is installed, you get full RTC scheduling:
        # get_action_queue(execution_horizon=10, enabled=True)
    """
    try:
        from lerobot.common.policies.rtc import ActionQueue, RTCConfig  # type: ignore[import]
        cfg = RTCConfig(enabled=True, execution_horizon=execution_horizon, **kwargs)
        queue = ActionQueue(cfg)
        logger.info(
            "lerobot_bridge: using LeRobot ActionQueue (horizon=%d)", execution_horizon
        )
        return queue
    except ImportError:
        from .action_chunk_buffer import ActionChunkBuffer
        logger.debug(
            "lerobot_bridge: lerobot not installed -- using ActionChunkBuffer (horizon=%d)",
            execution_horizon,
        )
        return ActionChunkBuffer(execution_horizon=execution_horizon)
    except Exception as exc:  # grain: ignore NAKED_EXCEPT -- lerobot API version drift -- any SDK error is a fallback signal
        logger.warning(
            "lerobot_bridge: LeRobot import failed (%s) -- falling back to ActionChunkBuffer",
            exc,
        )
        from .action_chunk_buffer import ActionChunkBuffer
        return ActionChunkBuffer(execution_horizon=execution_horizon)
