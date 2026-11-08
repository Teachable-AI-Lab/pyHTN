from pyhtn.common.imports.typing import *
from abc import ABC, abstractmethod
from pyhtn.common.utils import rand_uid
from pyhtn.htn.matching import (
    to_logic,
    to_factset,
    head_binding,
    ground_head,
    iter_bindings,
    resolve_all,
)


def _pick(new, old, new_name, old_name):
    ''' Resolve a renamed keyword argument (e.g. pre= vs. preconditions=). '''
    if(new is not None and old is not None):
        raise TypeError(f"Pass only one of '{new_name}' or '{old_name}'.")
    return new if new is not None else old


# ------------------------------------------------------
# : HTN_Element

class HTN_Element(ABC):
    def __init__(self,
                 name: str,
                 args: Sequence[Any] = (),
                 cost=1.0) -> None:

        self.name = name
        self.args = tuple(args)
        self.cost = cost

    @property
    def arity(self):
        return len(self.args)

    @abstractmethod
    def __str__(self):
        raise NotImplementedError('Subclasses must implement this method.')

    @abstractmethod
    def __repr__(self):
        raise NotImplementedError('Subclasses must implement this method.')

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return self.id == other.id

    def _args_str(self):
        return ', '.join([repr(x) for x in self.args])


class MatchableMixin(ABC):
    def _get_match_bindings(self, task_exec, state, conditions):
        ''' Unify this element's head with a TaskEx's match, then extend the
            binding with every match of `conditions` (a cre Logic or None)
            against the state.
        '''
        binding = head_binding(self.args, task_exec.match)
        if(binding is None):
            return []
        fs = to_factset(state)
        binding = ground_head(self.args, binding, fs)
        if(binding is None):
            return []
        return list(iter_bindings(conditions, fs, binding))


# ------------------------------------------------------
# : Operator

class Operator(HTN_Element, MatchableMixin):
    ''' A primitive action, executed by the environment.

    -- Examples --
        x, y = Var(Block, 'x'), Var(Block, 'y')
        Operator("unstack", x, y,
            pre=[x.on == y, x.clear == True],
            eff=y.clear == True)

    Args can be given positionally or with args=[...]. `pre` and `eff`
    accept a cre Logic, a single literal, or a list of literals (an implicit
    AND). `preconditions` and `effects` are accepted as aliases.
    '''
    def __init__(self,
                 name : str = None,
                 *_args : Any,
                 args: Sequence[Any] = (),
                 pre=None,
                 eff=None,
                 preconditions=None,
                 effects=None,
                 cost=1.0) -> None:

        if(len(args) == 0 and _args):
            args = _args

        super().__init__(name, args, cost)
        self.id = f"O_{rand_uid()}"

        self.preconditions = to_logic(_pick(pre, preconditions, 'pre', 'preconditions'))
        self.effects = to_logic(_pick(eff, effects, 'eff', 'effects'))

    @property
    def pre(self):
        return self.preconditions

    @property
    def eff(self):
        return self.effects

    def __str__(self):
        prefix = f"Operator({self.name!r}" if self.name is not None else "Operator(??"
        if(len(self.args) > 0):
            return f"{prefix}, {self._args_str()})"
        return f"{prefix})"

    __repr__ = __str__

    def get_match_executions(self, task_exec, state):
        from pyhtn.htn.element_executions import OperatorEx

        bindings = self._get_match_bindings(task_exec, state, self.preconditions)
        return [
            OperatorEx(
                self, state,
                resolve_all(self.args, b),
                parent_task_exec=task_exec,
            )
            for b in bindings
        ]


# ------------------------------------------------------
# : Task

class Task(HTN_Element, MatchableMixin):
    ''' A reference to a task: either a root task or a subtask in a Method.

    -- Annotations (at most one) --
        optional=True    : the task may be skipped.
        optional_if=cond : the task may be skipped when `cond` matches.
        skip_if=cond     : the task is skipped when `cond` matches.
        block_if=cond    : (not implemented yet)
    '''
    def __init__(self,
                 name: str,
                 *_args : Any,
                 args: Sequence[Any] = (),
                 cost : float = 1.0,
                 priority : str = 'first',
                 repeat : int = 1,
                 optional : bool = False,
                 optional_if=None,
                 skip_if=None,
                 block_if=None):

        optional_if = to_logic(optional_if)
        skip_if = to_logic(skip_if)
        block_if = to_logic(block_if)

        n_annotations = (bool(optional) + (optional_if is not None) +
                         (skip_if is not None) + (block_if is not None))
        if(n_annotations > 1):
            raise ValueError("Task may only be annotated with at most one of 'optional', 'skip_if', 'optional_if', 'block_if'.")

        if(len(args) == 0 and _args):
            args = _args

        super().__init__(name, args, cost)
        self.id = f"T_{rand_uid()}"

        self.domain_key = f'{name}/{len(args)}'
        self.priority = priority
        self.repeat = repeat
        self.optional = optional
        self.optional_if = optional_if
        self.skip_if = skip_if
        self.block_if = block_if
        if(block_if is not None):
            raise NotImplementedError("'block_if' annotation not implemented yet.")

    def as_task_exec(self, state):
        '''Create a task execution from a task by taking its
            arguments as its match and fixing it to a state.
        '''
        from pyhtn.htn.element_executions import TaskEx
        return TaskEx(self, state, self.args)

    def __str__(self):
        if(len(self.args) > 0):
            return f"Task({self.name!r}, {self._args_str()})"
        return f"Task({self.name!r})"

    __repr__ = __str__

    def get_child_executions(self, domain, state):
        task_exec = self.as_task_exec(state)
        return task_exec.get_child_executions(domain, state)

    def fn_str(self):
        return f"{self.name}({', '.join([str(x) for x in self.args])})"


# ------------------------------------------------------
# : Method

class Unord:
    ''' A group of subtasks that may be executed in any order. '''
    def __init__(self, *args):
        self.items = args


class Method(HTN_Element, MatchableMixin):
    ''' A way of decomposing a task into subtasks.

    -- Examples --
        x, dest = Var(Block, 'x'), Var(Block, 'dest')
        Method("move", x, dest,
            pre=[x.clear == True, dest.clear == True],
            subtasks=[Task("unstack", x, x.on), Task("stack", x, dest)])

    Vars in subtasks are bound by the head (the task's args) or by matching
    `pre`. A deref like `x.on` is evaluated on the fact bound to `x`.
    '''
    def __init__(self,
                 name: str,
                 *_args : Any,
                 args: Sequence[Any] = (),
                 subtasks: Union[Sequence[Union[Task, Unord]], Unord] = (),
                 pre=None,
                 preconditions=None,
                 cost=1.0) -> None:

        if(len(args) == 0 and _args):
            args = _args

        super().__init__(name, args, cost)
        self.id = f"M_{rand_uid()}"

        if(isinstance(subtasks, (Unord, Task))):
            subtasks = [subtasks]

        _subtasks = []
        _unord_spans = []

        for subtask in subtasks:
            if(isinstance(subtask, Unord)):
                s = len(_subtasks)
                _unord_spans.append( (s, s+len(subtask.items)) )
                _subtasks += subtask.items
            else:
                _subtasks.append(subtask)

        for st in _subtasks:
            if(not isinstance(st, Task)):
                raise TypeError(f"Method subtasks must be Task or Unord, got {st!r}.")

        self.subtasks = _subtasks
        self.unord_spans = _unord_spans
        self.preconditions = to_logic(_pick(pre, preconditions, 'pre', 'preconditions'))

    @property
    def pre(self):
        return self.preconditions

    def __str__(self):
        if(len(self.args) > 0):
            s = f"Method({self.name!r}, {self._args_str()}"
        else:
            s = f"Method({self.name!r}"

        s += f", subtasks={self.subtasks})"
        return s

    __repr__ = __str__

    def get_match_executions(self, task_exec, state):
        ''' Get all Method exectutions that match the Method's preconditions
            given a parent Task execution.
        '''
        from pyhtn.htn.element_executions import MethodEx, TaskEx

        bindings = self._get_match_bindings(task_exec, state, self.preconditions)

        meth_execs = []
        for b in bindings:
            meth_exec = MethodEx(
                self, state,
                resolve_all(self.args, b),
                parent_task_exec=task_exec,
            )
            meth_exec.child_execs = [
                TaskEx(
                    subtask, state,
                    resolve_all(subtask.args, b),
                    parent_method_exec=meth_exec,
                )
                for subtask in self.subtasks
            ]
            meth_execs.append(meth_exec)

        return meth_execs
