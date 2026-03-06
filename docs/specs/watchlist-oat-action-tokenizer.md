# Watchlist: OAT (Open Action Tokenizer)

**Status:** Research paper only (Feb 2026). No public implementation yet.
**Priority:** HIGH -- directly impacts our low-latency actuation strategy.
**Paper:** [arxiv.org/html/2602.21157v1](https://arxiv.org/html/2602.21157v1)
**Reported:** [marktechpost.com/2026/02/08](https://marktechpost.com/2026/02/08)

---

## What OAT Is

OAT (Open Action Tokenizer) brings LLM-style token scaling to robot action generation.
The key insight: **you don't need to wait for the full action chunk before executing**.

### Core Properties

**Anytime inference**
Early tokens in the sequence represent coarse, valid motor commands. As more tokens
arrive they progressively refine the action. Execution can begin on the first few
tokens -- the robot starts moving ~10-50ms after inference begins, not after the
full chunk decodes (~200ms).

**Total decodability**
Every partial token sequence decodes to a valid motor command. There are no invalid
intermediate states -- every prefix is executable. This is the property that enables
anytime inference.

**Causal ordering**
Early tokens represent coarser, earlier actions. Later tokens refine the command
in place. The architecture mirrors how autoregressive LLMs produce tokens: first
tokens carry the most information about the immediate next action.

**LLM-style scaling**
OAT applies the same scaling laws observed in language models to action tokenization.
Larger token budgets = higher fidelity actions. Smaller budgets = faster, coarser
actions. The budget can be tuned per-deployment depending on latency constraints.

---

## Why This Matters for OpenClaw Embodiment SDK

### Current Actuation Latency (v2.2)

```
LLM inference begins
        |
        |  ~100-300ms (full inference)
        v
Full K-step chunk decoded
        |
        v
ActionChunkBuffer.merge(chunk) called
        |
        v  ~10ms (buffer overhead)
Robot starts executing
```

**Total latency from decision to motion: ~200-300ms**

### With OAT

```
LLM inference begins
        |
        |  ~10-50ms (first token)
        v
First few tokens available (coarse action)
        |
        v  ~1ms (streaming decode)
Robot starts executing coarse action
        |
        |  Tokens continue arriving...
        v
Action progressively refined in-flight
```

**Total latency from decision to motion: ~10-50ms**

For the Go2 quadruped and Reachy 2 humanoid, this is the difference between:
- Smooth reactive motion in fast tasks (OAT)
- Jerky or delayed motion with visible "think-then-move" latency (current)

### Concrete Impact by Platform

| Platform | Current latency | OAT latency | Motion quality impact |
|----------|----------------|-------------|----------------------|
| Unitree Go2 | ~200ms | ~15ms | Smooth reactive gait; real-time obstacle response |
| Reachy 2 | ~250ms | ~20ms | Fluid arm motion; real-time human interaction |
| Reachy Mini | ~180ms | ~12ms | Responsive head tracking; smooth expressions |

---

## Integration Plan (When OAT SDK Drops)

### Phase 1: Drop-in at Buffer Level

OAT is a drop-in replacement for `ActionChunkBuffer` -- the existing `execute_chunk()` API
stays unchanged at the HAL layer. Only the buffer implementation changes.

```python
# Current (v2.2)
from openclaw_embodiment.hal.lerobot_bridge import get_action_queue
queue = get_action_queue(execution_horizon=10)  # ActionChunkBuffer or LeRobot

# Future (OAT)
from openclaw_embodiment.hal.oat_bridge import get_oat_queue  # future file
queue = get_oat_queue(token_budget=32)  # OATActionQueue or fallback
```

### Phase 2: Streaming Chunk API

Add `start_streaming_chunk(token_stream)` to `ActuatorHal` ABC:

```python
def start_streaming_chunk(self, token_stream) -> None:
    """Begin executing coarse actions immediately from a token stream.
    
    As tokens arrive from the LLM, the OAT decoder produces progressively
    refined commands. Execution begins on the first decodable prefix.
    
    Args:
        token_stream: Async iterator of action tokens from LLM.
    """
    ...
```

### Phase 3: Progressive Refinement in ActionChunkBuffer

Modify `ActionChunkBuffer.merge()` to accept partial chunks / progressive refinements:

```python
def merge(
    self,
    new_chunk: list[ActuatorCommand],
    inference_delay_steps: int = 4,
    partial: bool = False,  # NEW -- if True, treat as progressive refinement
) -> None:
    ...
```

### Phase 4: OATActuatorHal Wrapper

New wrapper HAL that accepts partial token streams and decodes them on-the-fly:

```python
class OATActuatorHal(ActuatorHal):
    """Wraps any ActuatorHal with OAT streaming decode.
    
    Begins executing coarse actions as soon as first tokens arrive.
    Refines in-flight as more tokens decode.
    """
    def __init__(self, base_hal: ActuatorHal, token_budget: int = 32): ...
    def start_streaming_chunk(self, token_stream) -> None: ...
```

---

## What to Watch For

### HuggingFace
- Watch for: `oat-action-tokenizer`, `open-action-tokenizer`, or similar repo
- Authors from the paper: monitor their HuggingFace profiles
- Expected release pattern: paper → reference implementation (2-6 months typical)

### Integration Signals
When a public Python implementation appears:
1. Check if it wraps around a standard tokenizer interface
2. Verify `anytime_decode(partial_tokens) -> ActuatorCommand` API exists
3. Check PyPI: `pip install oat-action-tokenizer` or `pip install lerobot[oat]`

### Dependency Strategy
Following our One-In-One-Out rule:
- If OAT ships standalone: add as optional dep alongside lerobot
- If OAT ships as part of lerobot: automatic via existing lerobot_bridge
- If OAT replaces lerobot for our use case: retire ActionChunkBuffer, keep lerobot_bridge

---

## Tracking

| Item | Details |
|------|---------|
| Paper | [arxiv 2602.21157](https://arxiv.org/html/2602.21157v1) |
| Status | Research paper only -- Feb 2026 |
| Implementation | None (as of Feb 2026) |
| Watch | HuggingFace for `oat-action-tokenizer` |
| SDK readiness | `execute_chunk()` API is OAT-ready; only buffer layer needs swap |
| Priority | High -- first-token latency is the key bottleneck for real-time tasks |

---

*Added: 2026-03-06 | SDK version: v2.2.0*
