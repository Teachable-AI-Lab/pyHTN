# PyHTN (Python HTN Planner)

## Table of Contents
- [Overview](#overview)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Building an HTN](#building-an-htn)
  - [State and Fact Types](#state-and-fact-types)
  - [Variables and Conditions](#variables-and-conditions)
  - [Operators](#operators)
  - [Methods](#methods)
  - [Tasks](#tasks)
  - [The HTN Container](#the-htn-container)
  - [Dictionary Format](#dictionary-format)
- [The Environment](#the-environment)
- [Using the Planner](#using-the-planner)
  - [Known Limitations](#known-limitations)
- [Running the Tests](#running-the-tests)

## Overview

This project implements a reactive, open-world Hierarchical Task Network (HTN) planner in Python.
HTN planning decomposes high-level tasks into simpler subtasks using a hierarchy of methods and operators.
Unlike closed-world planning, where HTNs generally have knowledge of how operators effect the world and can therefore directly modify the world state, open-world
planning systems generally assume latent actors in the world, which means it's not easy to know exactly how an agent's actions affect the world. This planner is reactive
in that actions (operators) are executed in the world as they are encountered, the world state is refreshed, and the HTN determines the next appropriate course of action, backtracking if necessary.

This planner supports:
- Hierarchical task decomposition, including unordered subtask groups
- Optional and conditionally skipped subtasks
- Reacting to the world state after every operator
- Execution tracing and logging

Backtracking and interactive (step-by-step method selection) planning are
still in progress; see [Known Limitations](#known-limitations).

Preconditions are matched with [cre](https://github.com/DannyWeitekamp/CCRE), a
fast C++ rule engine with Python bindings.

This project draws inspiration from [SHOP2 (Simple Hierarchical Ordered Planner No. 2)](https://www.cs.umd.edu/~nau/papers/nau2003shop2.pdf).

## Installation

PyHTN requires Python 3.12+. Install it from GitHub with pip. This also
builds `cre` from source, which needs CMake, Ninja and a C++23 compiler:

```bash
pip install git+https://github.com/Teachable-AI-Lab/pyHTN.git
```

## Quick Start

```python
from cre import Var, FactType
from pyhtn.htn import HTN, HTN_Planner, Method, Operator, Task

# Fact types: the schema of the world state
Block = FactType("Block", {
    "id": {"type": str, "unique_id": True},
    "on": "Block",          # a reference to another Block (absent = on the table)
    "clear": bool,
})

x, y, dest = Var(Block, "x"), Var(Block, "y"), Var(Block, "dest")

blocks = HTN(
    # Operators are primitive actions, executed by the environment.
    Operator("unstack", x, y, pre=[x.on == y, x.clear == True]),
    Operator("stack", x, dest, pre=[x.clear == True, dest.clear == True]),

    # Methods decompose a task. They're tried in the order given.
    Method("move", x, dest, pre=x.on == dest, subtasks=[]),   # already there
    Method("move", x, dest,
        pre=[x.on == y, x.clear == True, dest.clear == True],  # binds y
        subtasks=[Task("unstack", x, y), Task("stack", x, dest)]),
)

planner = HTN_Planner(blocks, tasks=[Task("move", "a", "c")], env=env)

while not planner.is_exhausted():
    planner.plan_to_next_operators()
    op_execs = planner.get_next_operator_execs()
    planner.apply(op_execs[0])     # executes the operator in `env`
```

See [The Environment](#the-environment) for what `env` needs to provide.

## Building an HTN

### State and Fact Types

The world state is a cre `FactSet`. Each fact is an instance of a cre
`FactType`, which declares its members and their types:

```python
Block = FactType("Block", {
    "id": {"type": str, "unique_id": True},
    "on": "Block",
    "clear": bool,
})
```

Environments may return a `FactSet` directly, or a list of dicts that each
name their type with a `"type"` key. A member that references another fact
uses `"@<unique id>"`:

```python
[{"type": "Block", "id": "a", "on": "@b", "clear": True},
 {"type": "Block", "id": "b", "clear": False}]
```

Fact types are found by name, so they only need to be defined somewhere
before the state is read.

### Variables and Conditions

Variables are cre `Var`s. A typed var (`Var(Block, "x")`) binds to a fact; an
untyped var (`Var("eq")`) binds to any value. Attribute access derefs
members, and comparisons build conditions:

```python
x.clear == True            # x's clear member is True
x.on == y                  # x is on y
x.on.clear == False        # the block x is on isn't clear
```

Anywhere a condition is accepted (`pre`, `eff`, `optional_if`, `skip_if`) you
can pass a single condition, a list of conditions (an implicit AND), or any cre
Logic built with `AND`, `OR`, `Not`, `Exists`, etc.

### Operators

An operator is a primitive action that the environment executes:

```python
Operator("unstack", x, y,
    pre=[x.on == y, x.clear == True],     # optional preconditions
    eff=y.clear == True)                  # optional; descriptive
```

Operators are applied in the environment, which then reports the new state,
so `eff` is just a description. The planner doesn't use it to update the
state.

### Methods

A method decomposes a task into subtasks:

```python
Method("move", x, dest,
    pre=[x.on == y, x.clear == True],
    subtasks=[Task("unstack", x, y), Task("stack", x, dest)])
```

- The head args (`x, dest`) are unified with the task's args. Constants in
  the head must match exactly.
- `pre` is matched against the state with the head vars pinned. Vars that
  aren't in the head (like `y` above) are bound by the match, and every match
  produces a separate method execution.
- Subtask args can use any bound var, or a deref like `x.on`, which is
  evaluated on the fact bound to `x`.
- `subtasks=[]` makes a method that accomplishes its task by doing nothing
  (for example, when the goal already holds).

Wrap subtasks in `Unord(...)` to allow any order:

```python
Method("S", subtasks=[
    Task("a"),
    Unord(Task("b"), Task("c"), Task("d")),
    Task("e"),
])
```

### Tasks

`Task(name, *args)` refers to a task, either as a root task given to the
planner or as a subtask in a method. Args may be values, vars or derefs. A
plain value given for a fact-typed var is treated as a unique id, so
`Task("move", "a", "c")` refers to the blocks with ids `"a"` and `"c"`.

A task can carry at most one of these annotations:

| Annotation | Meaning |
|---|---|
| `optional=True` | The task may be skipped. |
| `optional_if=cond` | The task may be skipped when `cond` matches. |
| `skip_if=cond` | The task is skipped when `cond` matches. |

Conditions on a subtask are matched with the subtask's args bound, so they can
refer to the method's vars:

```python
Task("unstack", x, y, skip_if=y.id == "table")
```

### The HTN Container

`HTN(*elements)` groups methods and operators by task name and keeps their
order. It checks that:
- every element for a task takes the same number of args;
- no task has both operators and methods;
- subtasks are called with the right number of args.

It warns about subtasks that nothing defines yet. That's allowed, since methods
can be added later.

```python
htn = HTN(*operators, *methods)
htn.add(Method("move", x, dest, subtasks=[]))   # add more later
htn.remove(some_method)
combined = htn + other_htn
htn["move"]                                     # [Method, Method, ...]
```

### Dictionary Format

A domain can also be given as a dict of `{task name: [Method | Operator, ...]}`.
`HTN_Planner` accepts either form, and `HTN.from_dict(d)` converts one
explicitly. Elements are filed under their dict key.

```python
domain = {
    "stack": [Operator("stack", x, dest, pre=[x.clear == True, dest.clear == True])],
    "move":  [Method("move", x, dest, subtasks=[Task("stack", x, dest)])],
}
```

## The Environment

The planner talks to the world through an environment object with two
methods:

```python
class Env:
    def get_state(self):
        ''' Return the current state, as a cre FactSet or a list of dicts
            with "type" keys. '''

    def execute_action(self, name, args) -> bool:
        ''' Execute the operator `name`. `args` are its bound args; fact-typed
            args are cre Facts (use fact.get_unique_id() for their ids).
            Return True on success. '''
```

After each operator, the planner reads the state again, so later subtasks are
matched against the world as it actually is.

## Using the Planner

```python
from pyhtn.htn import HTN_Planner

planner = HTN_Planner(htn, tasks=[Task("move", "a", "c")], env=env,
                      enable_logging=False)
```

Root tasks can be given as `Task`s, as `(name, *args)` tuples, or as
`{"name": ..., "args": [...]}` dicts. They run in order of `priority`
(`"first"`, `"high"`, `"medium"`, `"low"`, or a number; lower runs sooner,
default `"first"`), and tasks with the same priority run in the order they
were added. A root task may be a method task or an operator.

| Method | Description |
|---|---|
| `plan_to_next_operators()` | Plan until the next executable operators. Every operator that could come next is found, for example each member of an `Unord` group or each match of an operator's preconditions. |
| `get_next_operator_execs()` | The operator executions found by the last call. |
| `apply(op_exec)` | Execute an operator in the environment and advance the plan. Returns `False` if the environment reports failure. |
| `plan(multiheaded=True)` | Plan to the end, applying the first executable operator at each step. |
| `is_exhausted()` | True when every root task has been planned. |
| `add_tasks(tasks)` | Add root tasks. |
| `add_method(method, task=None)` | Add a method to the HTN. |
| `get_prev_operator_exec()` | The last operator execution applied. |
| `get_current_plan()` | The operator executions applied so far, in order (failed ones excluded). |
| `print_current_plan()` | Print the operators applied so far. |
| `get_current_root()` | The root `TaskEx` being planned, or `None`. |
| `clear_tasks()` | Drop all queued root tasks and the one in progress. |
| `reset()` | Drop the root task in progress and start a new trace. Queued root tasks are kept. |
| `print_planner_state()` | Print the queued root tasks and cursors. |
| `print_current_frames()` | Print the current planning frames. |
| `print_network()` | Print the HTN. |
| `print_current_trace()` | Print the execution trace. |

Each `OperatorEx`, `MethodEx` and `TaskEx` links to its parent
(`parent_exec`) and children (`child_execs`). `tree_to_dict()` on a root
`TaskEx` serializes the execution tree, and
`pyhtn.htn.element_executions.tree_dict_to_str()` renders it.

### Known Limitations

These are tracked by `xfail` tests in `tests/test_planner.py`. A proposed
design for fixing them is in
[docs/plans/multiheaded-reevaluation.md](docs/plans/multiheaded-reevaluation.md).

- **Re-planning and operator failure:** calling `plan_to_next_operators()`
  again without a successful `apply()` re-executes the pending operators
  and crashes, instead of re-evaluating them against the current state.
  Single-headed backtracking isn't implemented.
- **Tasks with no matches:** a task with no matching method or operator
  raises an error, instead of dropping that branch.
- **Single-headed and interactive planning:** `plan(multiheaded=False)` and
  `plan_to_next_decomposition()` don't work yet, so the interactive
  method-selection calls (`get_next_method_execs()`, `stage_method_exec()`,
  `add_method_exec()`) can't be used as intended.

## Running the Tests

```bash
pip install -e .
pytest
```
