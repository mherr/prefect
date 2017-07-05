from collections import namedtuple
from contextlib import contextmanager
import logging
import prefect
from prefect import signals
from prefect.state import TaskRunState
# from prefect.runners.results import RunResult, Progress
from prefect.runners.runner import Runner
import sys
import traceback
import types
import uuid


class TaskRunner(Runner):

    def __init__(self, task, executor=None, progress_fn=None):
        self.task = task
        super().__init__(
            executor=executor,
            logger_name=repr(task),
            progress_fn=progress_fn)

    def run(
            self,
            state=None,
            upstream_states=None,
            inputs=None,
            context=None,
            success_result=None):
        """
        Run a task

        Arguments
            state (TaskRunState): the task's current state

            upstream_states (dict): a dictionary of {task.name: TaskRunState}
                pairs containing the states of any upstream tasks

            inputs (dict): a dictionary of {kwarg: value} pairs containing
                inputs to the task function

            success_result (any): if the task doesn't run because it already
                succeeded, this result will be returned. This can be used when
                rerunning a group of tasks that may have returned results.
        """
        state = prefect.state.TaskRunState(state)
        upstream_states = upstream_states or {}
        inputs = inputs or {}

        result = success_result if state.is_successful() else None

        with prefect.context(context):
            with self.catch_signals(state):
                state = self.check_state(
                    state=state,
                    upstream_states=upstream_states)
                result = self.run_task(inputs=inputs)
                state, result = self.finalize(state, result)

        return dict(state=state, result=result)

    @contextmanager
    def catch_signals(self, state):
        try:
            yield

        except signals.SUCCESS as s:
            self.logger.info(f'TaskRun {type(s).__name__}: {s}')
            state.succeed()
        except signals.SKIP as s:
            self.logger.info(f'TaskRun {type(s).__name__}: {s}')
            state.skip()
        except signals.RETRY as s:
            self.logger.info(f'TaskRun {type(s).__name__}: {s}')
            state.fail()
        except signals.SHUTDOWN as s:
            self.logger.info(f'TaskRun {type(s).__name__}: {s}')
            state.shutdown()
        except signals.DONTRUN as s:
            self.logger.info(f'TaskRun {type(s).__name__}: {s}')
        except signals.FAIL as s:
            self.logger.info(f'TaskRun {type(s).__name__}: {s}')
            state.fail()
        except Exception as e:
            self.logger.error(
                'TaskRun: An unexpected error occurred', exc_info=1)
            if prefect.context.get('debug'):
                raise
            state.fail()

    def check_state(self, state, upstream_states, context=None):
        """
        Check if a Task is ready to run
        """

        with prefect.context(context):

            # -------------------------------------------------------------
            # check upstream tasks
            # -------------------------------------------------------------

            # make sure all upstream tasks are finished
            if not all(s.is_finished() for s in upstream_states.values()):
                raise signals.DONTRUN('Upstream tasks are not finished.')

            # check the task trigger function
            elif not self.task.trigger(upstream_states):
                raise signals.DONTRUN('Trigger failed')

            # -------------------------------------------------------------
            # check this task
            # -------------------------------------------------------------

            # this task is already running
            if state.is_running():
                raise signals.DONTRUN('TaskRun is already running.')

            # this task is already finished
            elif state.is_finished():
                raise signals.DONTRUN('TaskRun is already finished.')

            # this task is not pending
            elif not state.is_pending():
                raise signals.DONTRUN(
                    f'TaskRun is not ready to run (state {state}).')

            # -------------------------------------------------------------
            # start!
            # -------------------------------------------------------------

            state.start()

        return state

    def run_task(self, inputs=None, context=None):
        """
        Execute a Task

        Arguments
            inputs: a dictionary of inputs to the Task

        """

        with prefect.context(context):

            try:
                result = self.task.run(**inputs)

                # tasks can yield progress
                if isinstance(result, types.GeneratorType):

                    # use a sentinel to get the task's final result
                    sentinel = uuid.uuid1().hex

                    def sentinel_wrapper(task_generator):
                        task_result = yield from task_generator
                        yield {sentinel: task_result}

                    for progress in sentinel_wrapper(result):

                        # if we see the sentinel, this is the task's return value
                        if isinstance(progress, dict) and sentinel in progress:
                            result = progress[sentinel]
                            break

                        self.record_progress(progress)
            except signals.PrefectSignal:
                raise
            except Exception as e:
                raise signals.FAIL(traceback.format_exc())


        return result

    def finalize(self, state, result):
        state.succeed()
        return state, result