import copy
from typing import Iterable, Mapping

from slugify import slugify

import prefect
import prefect.context
from prefect.flows.schedules import NoSchedule
from prefect.core.task import Task
from prefect.core.edge import Edge
from prefect.core.parameter import Parameter
from prefect.utilities.strings import is_valid_identifier
from prefect.utilities.serialize import Serializable
import uuid


class Flow(Serializable):

    _serialized_restore_fields = ['tasks', 'edges']

    def __init__(
            self,
            name='Flow',
            id=None,
            version=None,
            schedule=NoSchedule(),
            concurrent_runs=None,  #TODO
            description=None):
        """
        Args:
            schedule (prefect.Schedule): a Schedule object that returns the
                Flow's schedule

            cluster (str): The address of a specific cluster that this Flow
                should run in. If not provided, the default cluster will be
                used.

        """

        self.name = name
        self.version = version
        self.description = description
        self.id = id or uuid.uuid4().hex

        self.schedule = schedule
        self.tasks = dict()
        self.edges = set()
        self.concurrent_runs = concurrent_runs
        self.cluster = cluster

    @property
    def slug(self):
        if self.version not in [None, '']:
            return slugify('{self.name}:{self.version}'.format(self=self))
        else:
            return slugify(self.name)

    def __repr__(self):
        base = self.name
        if self.version:
            base += ':{self.version}'.format(self=self)
        return "{type}('{base}')".format(type=type(self).__name__, base=base)

    def __hash__(self):
        return id(self)

    # Graph -------------------------------------------------------------------

    def __iter__(self):
        yield from self.sorted_tasks()

    def get_task(self, name):
        """
        Retrieve a task by name
        """
        if name in self.tasks:
            return self.tasks[name]
        else:
            raise ValueError('Task {} was not found in the Flow'.format(name))

    def add_task(self, task):
        if not isinstance(task, Task):
            raise TypeError(
                'Expected a Task; received {}'.format(type(task).__name__))
        self.tasks[task.id] = task

    def add_edge(self, upstream_task, downstream_task, key=None):
        """
        Adds an Edge to the Flow. Edges create dependencies between tasks.
        The simplest edge simply enforcces an ordering so that the upstream
        task runs before the downstream task, but edges can introduce more
        complex behaviors as well.
        """

        edge = Edge(
            upstream_task=upstream_task.id,
            downstream_task=downstream_task.id,
            key=key,
        )

        if upstream_task not in self.tasks.values():
            self.add_task(upstream_task)
        if downstream_task not in self.tasks.values():
            self.add_task(downstream_task)

        if edge.key is not None:
            existing_edges = [
                e for e in self.edges if
                e.downstream_task == downstream_task.id and e.key == edge.key
            ]
            if existing_edges:
                raise ValueError(
                    'An edge to task {edge.downstream_task} with '
                    'key "{edge.key}" already exists!'.format(edge=edge))

        self.edges.add(edge)

        # check that the edge doesn't add a cycle
        self.sorted_tasks()

    # Dependencies ------------------------------------------------------------

    def set_dependencies(
            self,
            task: Task,
            upstream_tasks: Iterable[Task] = None,
            downstream_tasks: Iterable[Task] = None,
            upstream_results: Mapping[str, Task] = None):
        """
        Convenience function for adding task dependencies on upstream tasks.

        Args:
            task (Task): a Task that will become part of the Flow

            upstream_tasks ([Task]): Tasks that will run before the task runs

            downstream_tasks ([Task]): Tasks that will run after the task runs

            upstream_results ({key: Task}): The results of these tasks
                will be provided to the task under the specified keyword
                arguments.
        """
        if task not in self.tasks.values():
            self.add_task(task)

        for t in upstream_tasks or []:
            self.add_edge(upstream_task=t, downstream_task=task)

        for t in downstream_tasks or []:
            self.add_edge(upstream_task=task, downstream_task=t)

        for key, t in (upstream_results or {}).items():
            t = prefect.utilities.tasks.as_task(t)
            self.add_edge(upstream_task=t, downstream_task=task, key=key)

    def upstream_tasks(self, task):
        """
        Set of all tasks immediately upstream from a task.

        Args:
            task (Task): tasks upstream from this task will be returned.
        """
        if isinstance(task, str):
            task_id = task
        else:
            task_id = task.id

        return set(
            self.get_task(e.upstream_task)
            for e in self.edges
            if e.downstream_task == task_id)

    def downstream_tasks(self, task):
        """
        Set of all tasks immediately downstream from a task.

        Args:
            task (Task): tasks downstream from this task will be returned.
        """
        if isinstance(task, str):
            task_id = task
        else:
            task_id = task.id

        return set(
            self.get_task(e.downstream_task)
            for e in self.edges
            if e.upstream_task == task_id)

    def sorted_tasks(self, root_tasks=None):
        """
        Returns a topological sort of this Flow's tasks.
        """

        # generate a list of all tasks downstream from root_tasks
        if root_tasks is not None:
            # recover tasks from root tasks (if they are strings)
            tasks = set(
                [
                    self.get_task(t) if isinstance(t, str) else t
                    for t in root_tasks
                ])
            seen = set()

            while seen != tasks:
                # for each task we haven't seen yet...
                for t in list(tasks.difference(seen)):
                    # add its downstream tasks to the list
                    tasks.update(self.downstream_tasks(t))
                    # mark it as seen
                    seen.add(t)
        else:
            tasks = set(self.tasks.values())

        sorted_tasks = []
        while tasks:
            acyclic = False
            for task in list(tasks):
                for upstream_task in self.upstream_tasks(task):
                    if upstream_task in tasks:
                        # the previous task hasn't been sorted yet, so
                        # this task can't be sorted either
                        break
                else:
                    # all previous tasks are sorted, so this one can be
                    # sorted as well
                    acyclic = True
                    tasks.remove(task)
                    sorted_tasks.append(task)
            if not acyclic:
                # no tasks matched
                raise ValueError('Flows must be acyclic!')

        return tuple(sorted_tasks)

    def edges_to(self, task):
        """
        Set of all Edges leading to this Task

        Args:
            task (Task or str)
        """
        if isinstance(task, str):
            task_id = task
        else:
            task_id = task.id
        return set(e for e in self.edges if e.downstream_task == task_id)

    def edges_from(self, task):
        """
        Set of all Edges leading from this Task

        Args:
            task (Task or str)
        """
        if isinstance(task, str):
            task_id = task
        else:
            task_id = task.id
        return set(e for e in self.edges if e.upstream_task == task_id)

    # Introspection -----------------------------------------------------------

    def root_tasks(self):
        """
        Returns the root tasks of the Flow -- tasks that have no upstream
        dependencies.
        """
        return set(t for t in self.tasks.values() if not self.edges_to(t))

    def terminal_tasks(self):
        """
        Returns the terminal tasks of the Flow -- tasks that have no downstream
        dependencies.
        """
        return set(t for t in self.tasks.values() if not self.edges_from(t))

    def parameters(self, only_required=False):
        """
        Returns the parameters of the flow, including whether they are required
        and any default value
        """
        return {
            t.name: {
                'required': t.required,
                'default': t.default
            }
            for t in self.tasks.values()
            if isinstance(t, Parameter) and (
                t.required if only_required else True)
        }

    def sub_flow(self, root_tasks=None):
        """
        Returns a Flow consisting of a subgraph of this graph including only
        tasks between the supplied root_tasks and ending_tasks.
        """

        sub_flow = copy.copy(self)
        sub_flow.tasks = dict()
        sub_flow.edges = set()

        for t in self.sorted_tasks(root_tasks=root_tasks):
            sub_flow.add_task(t)

        for e in self.edges:
            if e.upstream_task in sub_flow.tasks:
                if e.downstream_task in sub_flow.tasks:
                    sub_flow.edges.add(e)

        return sub_flow

    # Context Manager -----------------------------------------------

    def __enter__(self):
        self._previous_context = prefect.context.Context.as_dict()
        prefect.context.Context.update(dict(flow=self))
        return self

    def __exit__(self, _type, _value, _tb):
        prefect.context.Context.reset(self._previous_context)
        del self._previous_context

    # Persistence  ------------------------------------------------

    def serialize(self, pickle=False):

        serialized = super().serialize()

        tasks = self.sorted_tasks()
        if pickle:
            tasks = [t.serialize(pickle=pickle) for t in tasks]

        serialized.update(
            name=self.name,
            id=self.id,
            version=self.version,
            description=self.description,
            parameters=self.parameters(),
            schedule=self.schedule,
            concurrent_runs=self.concurrent_runs,
            tasks=tasks,
            edges=[e for e in self.edges])
        return serialized

    # Execution  ------------------------------------------------

    def run(
            self,
            parameters=None,
            executor=None,
            return_all_task_states=False,
            **kwargs):
        """
        Run the flow.
        """
        runner = prefect.engine.flow_runner.FlowRunner(
            flow=self, executor=executor)

        parameters = parameters or {}
        for p in self.parameters():
            if p in kwargs:
                parameters[p] = kwargs.pop(p)

        return runner.run(
            parameters=parameters,
            return_all_task_states=return_all_task_states,
            **kwargs)