# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import logging
from typing import Iterable, List, Optional

import torch
from pyre_extensions import none_throws
from torchtnt.framework._callback_handler import CallbackHandler
from torchtnt.framework.callback import Callback

from torchtnt.framework.state import ActivePhase, EntryPoint, PhaseState, State
from torchtnt.framework.unit import TEvalData, TEvalUnit
from torchtnt.framework.utils import (
    _is_epoch_done,
    _reset_module_training_mode,
    _set_module_training_mode,
    _step_requires_iterator,
    get_timing_context,
    log_api_usage,
)
from torchtnt.utils.timer import get_timer_summary, TimerProtocol

logger: logging.Logger = logging.getLogger(__name__)


def evaluate(
    eval_unit: TEvalUnit,
    eval_dataloader: Iterable[TEvalData],
    *,
    max_steps_per_epoch: Optional[int] = None,
    callbacks: Optional[List[Callback]] = None,
    timer: Optional[TimerProtocol] = None,
) -> None:
    """
    The ``evaluate`` entry point takes in a :class:`~torchtnt.framework.EvalUnit` object, a train dataloader (any Iterable), optional arguments to modify loop execution,
    and runs the evaluation loop.

    Args:
        eval_unit: an instance of :class:`~torchtnt.framework.EvalUnit` which implements `eval_step`.
        eval_dataloader: dataloader to be used during evaluation, which can be *any* iterable, including PyTorch DataLoader, DataLoader2, etc.
        max_steps_per_epoch: the max number of steps to run per epoch. None means evaluate until the dataloader is exhausted.
        callbacks: an optional list of :class:`~torchtnt.framework.Callback` s.
        timer: an optional Timer which will be used to time key events (using a Timer with CUDA synchronization may degrade performance).


    Below is an example of calling :py:func:`~torchtnt.framework.evaluate`.

    .. code-block:: python

        from torchtnt.framework.evaluate import evaluate

        eval_unit = MyEvalUnit(module=..., optimizer=..., lr_scheduler=...)
        eval_dataloader = torch.utils.data.DataLoader(...)
        evaluate(eval_unit, eval_dataloader, max_steps_per_epoch=20)

    Below is pseudocode of what the :py:func:`~torchtnt.framework.evaluate` entry point does.

    .. code-block:: text

        set unit's tracked modules to eval mode
        call on_eval_start on unit first and then callbacks
        while not done:
            call on_eval_epoch_start on unit first and then callbacks
            try:
                data = next(dataloader)
                call on_eval_step_start on callbacks
                call eval_step on unit
                increment step counter
                call on_eval_step_end on callbacks
            except StopIteration:
                break
        increment epoch counter
        call on_eval_epoch_end on unit first and then callbacks
        call on_eval_end on unit first and then callbacks
    """
    log_api_usage("evaluate")
    callback_handler = CallbackHandler(callbacks or [])
    state = State(
        entry_point=EntryPoint.EVALUATE,
        eval_state=PhaseState(
            dataloader=eval_dataloader,
            max_steps_per_epoch=max_steps_per_epoch,
        ),
        timer=timer,
    )
    try:
        _evaluate_impl(state, eval_unit, callback_handler)
        logger.info("Finished evaluation")
        if state.timer:
            logger.info(get_timer_summary(state.timer))
    except Exception as e:
        # TODO: log for diagnostics
        logger.info(e)
        eval_unit.on_exception(state, e)
        callback_handler.on_exception(state, eval_unit, e)
        raise e


@torch.no_grad()
def _evaluate_impl(
    state: State,
    eval_unit: TEvalUnit,
    callback_handler: CallbackHandler,
) -> None:
    # input validation
    eval_state = none_throws(state.eval_state)

    state._active_phase = ActivePhase.EVALUATE
    logger.info(
        f"Started evaluate with max_steps_per_epoch={eval_state.max_steps_per_epoch}"
    )

    # Set all modules to eval mode
    # access modules made available through AppStateMixin
    tracked_modules = eval_unit.tracked_modules()
    prior_module_train_states = _set_module_training_mode(tracked_modules, False)

    eval_unit.on_eval_start(state)
    callback_handler.on_eval_start(state, eval_unit)

    # Conditionally run this to avoid running this multiple times
    # in the case of resuming from a checkpoint mid-epoch
    if eval_unit.eval_progress.num_steps_completed_in_epoch == 0:
        eval_unit.on_eval_epoch_start(state)
        callback_handler.on_eval_epoch_start(state, eval_unit)

    with get_timing_context(state, "evaluate.iter(dataloader)"):
        data_iter = iter(eval_state.dataloader)
    step_input = data_iter

    pass_data_iter_to_step = _step_requires_iterator(eval_unit.eval_step)
    prev_steps_in_epoch = eval_unit.eval_progress.num_steps_completed_in_epoch

    while not (
        state.should_stop
        or _is_epoch_done(
            eval_unit.eval_progress,
            eval_state.max_steps_per_epoch,
            eval_state.max_steps,
        )
    ):
        try:
            if not pass_data_iter_to_step:
                # get the next batch from the data iterator
                with get_timing_context(state, "evaluate.next(data_iter)"):
                    step_input = next(data_iter)

            callback_handler.on_eval_step_start(state, eval_unit)
            eval_state._step_output = eval_unit.eval_step(state, step_input)

            eval_unit.eval_progress.increment_step()
            callback_handler.on_eval_step_end(state, eval_unit)

            # clear step_output to avoid retaining extra memory
            eval_state._step_output = None
        except StopIteration:
            break

    # Possibly warn about an empty dataloader
    any_steps_completed = (
        abs(eval_unit.eval_progress.num_steps_completed_in_epoch - prev_steps_in_epoch)
        > 0
    )
    if not any_steps_completed:
        logger.warning("No steps completed during evaluate epoch!")

    # set progress counters for the next epoch
    eval_unit.eval_progress.increment_epoch()

    eval_unit.on_eval_epoch_end(state)
    callback_handler.on_eval_epoch_end(state, eval_unit)

    eval_unit.on_eval_end(state)
    callback_handler.on_eval_end(state, eval_unit)

    # Reset training mode for modules at the end of the epoch
    # This ensures that side-effects made by the loop are reset before
    # returning back to the user
    _reset_module_training_mode(tracked_modules, prior_module_train_states)
