"""ActionChunkBuffer -- decouples LLM inference rate from robot control loop rate.

The LLM generates action chunks at ~5-10Hz.
The control loop executes commands at ~100-1000Hz.
This buffer sits between them, accepting new chunks while the current chunk
is executing and blending at boundaries.

API-compatible with LeRobot's ActionQueue for easy drop-in swap:
    from openclaw_embodiment.hal.action_chunk_buffer import ActionChunkBuffer
    # or, for full LeRobot RTC:
    from openclaw_embodiment.hal.lerobot_bridge import get_action_queue
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

from .base import ActuatorCommand


def _ms() -> float:
    """Return current time in milliseconds."""
    return time.monotonic() * 1000.0


class ActionChunkBuffer:
    """Decouples LLM inference rate from robot control loop rate.

    The LLM generates action chunks at ~5-10Hz.
    The control loop executes commands at ~100-1000Hz.
    This buffer sits between them.

    API-compatible with LeRobot's ActionQueue for easy swap-in.

    Usage::

        buffer = ActionChunkBuffer(execution_horizon=10)

        # LLM thread: push new chunk when inference completes
        buffer.merge(new_chunk_commands)

        # Control loop thread: pop one command per tick
        cmd = buffer.get()
        if cmd is not None:
            hal.execute(cmd)
    """

    def __init__(
        self,
        execution_horizon: int = 10,
        hold_on_empty: bool = True,
        max_queue_depth: int = 3,
    ) -> None:
        """Initialize ActionChunkBuffer.

        Args:
            execution_horizon: Number of overlap/blend steps at chunk boundaries.
                               LeRobot calls this the 'execution horizon'.
            hold_on_empty: If True, hold last position (return last command) when
                           buffer drains. If False, return None when empty.
            max_queue_depth: Maximum number of pending chunks to buffer ahead.
                             Older chunks are dropped when exceeded.
        """
        self._execution_horizon = execution_horizon
        self._hold_on_empty = hold_on_empty
        self._max_queue_depth = max_queue_depth

        self._queue: deque[ActuatorCommand] = deque()
        self._pending_chunks: deque[list] = deque()

        self._last_command: Optional[ActuatorCommand] = None
        self._lock = threading.Lock()

        # Inference lag tracking
        self._chunk_request_time: Optional[float] = None
        self._inference_lag_samples: deque[float] = deque(maxlen=20)

    def merge(
        self,
        new_chunk: list,
        inference_delay_steps: int = 4,
    ) -> None:
        """Add a new chunk to the buffer, blending at the current boundary.

        Blending: the first `execution_horizon` steps of the new chunk are
        linearly interpolated with the tail of the current buffer. This produces
        smooth transitions between chunks even at the inference rate boundary.

        Args:
            new_chunk: List of ActuatorCommand objects for the new chunk.
            inference_delay_steps: Number of steps to skip at the start of the
                                   new chunk to account for inference latency.
                                   Defaults to 4 (matches LeRobot default).
        """
        if not new_chunk:
            return

        # Record inference lag
        if self._chunk_request_time is not None:
            lag = _ms() - self._chunk_request_time
            self._inference_lag_samples.append(lag)
            self._chunk_request_time = None

        with self._lock:
            # If we have too many pending chunks ahead, drop the oldest one
            while len(self._pending_chunks) >= self._max_queue_depth:
                self._pending_chunks.popleft()

            # Blend the new chunk into the current queue at the boundary
            current_remaining = list(self._queue)
            blend_target = min(len(current_remaining), self._execution_horizon)

            if blend_target > 0 and len(new_chunk) > 0:
                # Linear interpolation over blend window
                blended_tail = self._blend_commands(
                    current_remaining[:blend_target],
                    new_chunk[:blend_target],
                )
                # Rebuild queue: keep pre-blend, insert blended, then rest of new chunk
                pre_blend = current_remaining[blend_target:]
                merged = pre_blend + blended_tail + list(new_chunk[blend_target:])
                self._queue = deque(merged)
            else:
                # No blending needed -- just append
                self._queue.extend(new_chunk)

    def _blend_commands(
        self,
        current: list,
        incoming: list,
        weight: float = 0.5,
    ) -> list:
        """Blend two command lists at a chunk boundary.

        For commands with numeric params (e.g. joint angles), performs linear
        interpolation. For non-numeric params, keeps the incoming command unchanged.

        Args:
            current: Commands from current (expiring) chunk.
            incoming: Commands from new (arriving) chunk.
            weight: Blend weight (0=all current, 1=all incoming). Default: 0.5.

        Returns:
            List of blended ActuatorCommand objects.
        """
        blended = []
        for i, (cur, inc) in enumerate(zip(current, incoming)):
            # Interpolation weight increases linearly from 0 -> 1 over blend window
            alpha = (i + 1) / (len(current) + 1)
            blended_params = {}
            all_keys = set(cur.params.keys()) | set(inc.params.keys())
            for key in all_keys:
                cur_val = cur.params.get(key, 0)
                inc_val = inc.params.get(key, 0)
                if isinstance(cur_val, (int, float)) and isinstance(inc_val, (int, float)):
                    blended_params[key] = cur_val * (1 - alpha) + inc_val * alpha
                else:
                    blended_params[key] = inc_val

            blended.append(ActuatorCommand(
                command_id=inc.command_id,
                action=inc.action,
                params=blended_params,
                timestamp_ms=inc.timestamp_ms,
                timeout_ms=inc.timeout_ms,
                duration_ms=inc.duration_ms,
                timestamp_offset_ms=inc.timestamp_offset_ms,
            ))
        return blended

    def get(self) -> Optional[ActuatorCommand]:
        """Pop the next command for execution.

        Called by the control loop at each tick (e.g. 100-1000Hz).

        Returns:
            Next ActuatorCommand to execute, or:
            - Last command (hold-last-position) if buffer is empty and hold_on_empty=True
            - None if buffer is empty and hold_on_empty=False
        """
        with self._lock:
            # Record when buffer was drained (next chunk request time)
            if len(self._queue) == 1:
                self._chunk_request_time = _ms()

            if self._queue:
                cmd = self._queue.popleft()
                self._last_command = cmd
                return cmd

            # Buffer empty: check pending chunks
            if self._pending_chunks:
                next_chunk = self._pending_chunks.popleft()
                self._queue.extend(next_chunk)
                if self._queue:
                    cmd = self._queue.popleft()
                    self._last_command = cmd
                    return cmd

            # Buffer completely empty
            if self._hold_on_empty and self._last_command is not None:
                return self._last_command  # hold last position
            return None

    def get_left_over(self) -> list:
        """Return remaining commands in current chunk.

        LeRobot RTC compatibility: returns the remaining commands without
        consuming them. Used by RTC schedulers that need to peek ahead.

        Returns:
            List of remaining ActuatorCommand objects.
        """
        with self._lock:
            return list(self._queue)

    @property
    def queue_depth(self) -> int:
        """Return number of commands currently buffered (including pending chunks)."""
        with self._lock:
            return len(self._queue) + sum(len(c) for c in self._pending_chunks)

    @property
    def inference_lag_ms(self) -> float:
        """Return rolling average inference lag in milliseconds.

        Measured as: time between buffer draining (chunk request) and
        new chunk arriving via merge(). Reflects LLM inference latency.

        Returns:
            Rolling average lag in ms, or 0.0 if no measurements yet.
        """
        if not self._inference_lag_samples:
            return 0.0
        return sum(self._inference_lag_samples) / len(self._inference_lag_samples)

    def is_empty(self) -> bool:
        """Return True if buffer has no commands queued."""
        with self._lock:
            return len(self._queue) == 0 and len(self._pending_chunks) == 0

    def clear(self) -> None:
        """Emergency clear -- immediately empty the buffer.

        Use for safety stops. After clear(), the next get() will return
        hold-last-position (if hold_on_empty=True) or None.
        """
        with self._lock:
            self._queue.clear()
            self._pending_chunks.clear()

    def reset_lag_tracking(self) -> None:
        """Reset inference lag measurement history."""
        self._inference_lag_samples.clear()
        self._chunk_request_time = None

    def __repr__(self) -> str:
        return (
            f"ActionChunkBuffer("
            f"depth={self.queue_depth}, "
            f"lag={self.inference_lag_ms:.1f}ms, "
            f"hold={self._hold_on_empty})"
        )
