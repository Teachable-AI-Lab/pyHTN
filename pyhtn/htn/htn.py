''' HTN: a container of Methods and Operators, grouped by task name.

    x, dest = Var(Block, 'x'), Var(Block, 'dest')
    blocks = HTN(
        Operator("stack", x, dest, pre=[x.clear == True, dest.clear == True]),
        Method("move", x, dest,
            pre=x.clear == True,
            subtasks=[Task("unstack", x, x.on), Task("stack", x, dest)]),
        Method("move", x, dest, pre=x.on == dest),
    )

Methods for a task are tried in the order they're given. An HTN acts like a
read-only mapping {task name -> [Method | Operator, ...]}, which is what the
planner consumes. HTN.from_dict() builds one from the older dict format.
'''
import warnings
from pyhtn.common.imports.typing import *
from typing import Iterator
from pyhtn.htn.htn_elements import HTN_Element, Method, Operator, Task


class HTN:
    def __init__(self, *elements: Union[Method, Operator],
                 warn_undefined: bool = True):
        self._table : Dict[str, List[Union[Method, Operator]]] = {}
        self.warn_undefined = warn_undefined
        self.add(*elements)

    # ----------------------
    # : Building

    def add(self, *elements: Union[Method, Operator],
            task: Optional[str] = None) -> "HTN":
        ''' Add Methods or Operators. Each is filed under its own name, or
            under `task` if given.
        '''
        for elem in elements:
            if(isinstance(elem, (list, tuple))):
                self.add(*elem, task=task)
                continue
            if(not isinstance(elem, (Method, Operator))):
                raise TypeError(f"HTN elements must be Method or Operator, got {elem!r}.")

            name = task if task is not None else elem.name
            self._check_compatible(name, elem)
            self._table.setdefault(name, []).append(elem)

        if(self.warn_undefined):
            self._warn_undefined_tasks()
        return self

    def remove(self, element: Union[Method, Operator]) -> None:
        for name, elems in list(self._table.items()):
            for i, e in enumerate(elems):
                if(e is element):
                    del elems[i]
                    if(len(elems) == 0):
                        del self._table[name]
                    return
        raise KeyError(f"{element!r} is not in this HTN.")

    def _check_compatible(self, name, elem):
        existing = self._table.get(name, [])
        if(len(existing) == 0):
            return
        first = existing[0]
        if(isinstance(elem, Operator) != isinstance(first, Operator)):
            raise ValueError(
                f"Task '{name}' can't have both Operators and Methods: "
                f"adding {elem!r} alongside {first!r}.")
        if(elem.arity != first.arity):
            raise ValueError(
                f"Task '{name}' takes {first.arity} args, but {elem!r} "
                f"takes {elem.arity}.")

    def _warn_undefined_tasks(self):
        for task in self.subtask_refs():
            elems = self._table.get(task.name)
            if(elems is None):
                # Allowed: methods for it may be added (or learned) later.
                warnings.warn(f"{task!r} refers to a task with no Methods or Operators.",
                              stacklevel=3)
            elif(elems[0].arity != task.arity):
                raise ValueError(
                    f"{task!r} has {task.arity} args, but task '{task.name}' "
                    f"takes {elems[0].arity}.")

    @classmethod
    def from_dict(cls, domain: Dict[str, Any], **kwargs) -> "HTN":
        ''' Build an HTN from {task name: Method | Operator | [Method | Operator]}.
            Elements are filed under their dict key.
        '''
        htn = cls(warn_undefined=False)
        for name, elems in domain.items():
            if(not isinstance(elems, (list, tuple))):
                elems = [elems]
            htn.add(*elems, task=name)
        htn.warn_undefined = kwargs.get('warn_undefined', True)
        if(htn.warn_undefined):
            htn._warn_undefined_tasks()
        return htn

    @classmethod
    def coerce(cls, domain) -> "HTN":
        if(isinstance(domain, HTN)):
            return domain
        if(isinstance(domain, dict)):
            return cls.from_dict(domain)
        raise TypeError(f"Expected an HTN or a dict, got {type(domain).__name__}.")

    # ----------------------
    # : Querying

    @property
    def elements(self) -> List[Union[Method, Operator]]:
        return [e for elems in self._table.values() for e in elems]

    def subtask_refs(self) -> Iterator[Task]:
        for elem in self.elements:
            if(isinstance(elem, Method)):
                yield from elem.subtasks

    def methods(self, name: str) -> List[Method]:
        return [e for e in self._table.get(name, []) if isinstance(e, Method)]

    def operators(self, name: str) -> List[Operator]:
        return [e for e in self._table.get(name, []) if isinstance(e, Operator)]

    def is_primitive(self, name: str) -> bool:
        elems = self._table.get(name)
        return bool(elems) and isinstance(elems[0], Operator)

    def __add__(self, other: "HTN") -> "HTN":
        new = HTN(warn_undefined=False)
        for src in (self, other):
            for name, elems in src.items():
                new.add(*elems, task=name)
        new.warn_undefined = self.warn_undefined and other.warn_undefined
        return new

    # Mapping interface (what the planner uses)
    def get(self, name, default=None):
        elems = self._table.get(name)
        return list(elems) if elems is not None else default

    def __getitem__(self, name):
        return list(self._table[name])

    def __contains__(self, name):
        return name in self._table

    def __iter__(self):
        return iter(self._table)

    def __len__(self):
        return len(self._table)

    def keys(self):
        return self._table.keys()

    def values(self):
        return [list(v) for v in self._table.values()]

    def items(self):
        return [(k, list(v)) for k, v in self._table.items()]

    def __repr__(self):
        n = sum(len(v) for v in self._table.values())
        return f"HTN({len(self._table)} tasks, {n} elements)"
