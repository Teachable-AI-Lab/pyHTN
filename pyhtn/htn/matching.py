''' Pattern matching for HTN elements, backed by cre (CCRE) Logic objects.

An element (Method / Operator) has a head (its args, a mix of cre Vars and
constants) and optional conditions (a cre Logic). Matching a TaskEx against
it happens in two steps:

  1. head_binding(): unify the element's head args with the TaskEx's
     match values, producing a binding {var alias -> value}.
  2. iter_bindings(): extend that binding with every match of the
     element's Logic against the state (a cre FactSet). Head bindings for
     vars the Logic uses are passed to cre as a `partial` so the match is
     pinned to them.

Bindings are plain dicts keyed by var alias (cre Vars overload `==`, so
they can't be compared or used as dict keys directly).
'''
from pyhtn.common.imports.typing import *
from typing import Iterator
from cre import Var, Logic, AND, FactSet, Fact, FactType


# ------------------------------------------------------
# : Coercion

def to_logic(cond) -> Optional[Logic]:
    ''' Normalize a condition spec to a cre Logic (or None for no condition).
        Accepts a Logic, a single literal/expression (e.g. `x.on == y`), or a
        list/tuple of these (an implicit AND).
    '''
    if(cond is None):
        return None
    if(isinstance(cond, Logic)):
        return cond
    if(isinstance(cond, (list, tuple))):
        if(len(cond) == 0):
            return None
        return AND(*[to_logic(c) if isinstance(c, (list, tuple)) else c
                     for c in cond])
    return AND(cond)


def to_factset(state) -> FactSet:
    ''' Coerce a state to a cre FactSet. A list of dicts is converted with
        FactSet.from_py(), so each dict needs a "type" key naming a
        cre FactType.
    '''
    if(isinstance(state, FactSet)):
        return state
    if(state is None):
        return FactSet()
    return FactSet.from_py(state)


# ------------------------------------------------------
# : Vars

def is_var(x) -> bool:
    return isinstance(x, Var)


def is_deref(x) -> bool:
    ''' True for a dereferenced var like `x.on`. '''
    return isinstance(x, Var) and len(x) > 0


def alias_of(v: Var) -> str:
    return v.get_alias()


def is_fact_typed(v: Var) -> bool:
    return isinstance(v.get_base_type(), FactType)


# ------------------------------------------------------
# : Head unification

def _unify(x, val, binding, skip_derefs=False):
    ''' Unify one element arg `x` (Var, constant, or tuple of these) with a
        value `val` from a TaskEx's match. Mutates and returns binding, or
        returns None on failure.
    '''
    if(binding is None):
        return None

    if(is_var(x)):
        if(is_deref(x)):
            if(skip_derefs):
                return binding
            raise ValueError(
                f"Dereferenced var {x!r} can't be used as a head argument.")
        # An unbound var on the task side leaves the element var unbound;
        #  it may still be bound by matching the element's conditions.
        if(is_var(val)):
            return binding
        a = alias_of(x)
        if(a in binding):
            return binding if _values_equal(binding[a], val) else None
        binding[a] = val
        return binding

    if(is_var(val)):
        return binding

    if(isinstance(x, tuple) and isinstance(val, tuple)):
        if(len(x) != len(val)):
            return None
        for xi, vi in zip(x, val):
            binding = _unify(xi, vi, binding, skip_derefs)
            if(binding is None):
                return None
        return binding

    return binding if _values_equal(x, val) else None


def _values_equal(a, b):
    if(isinstance(a, Fact) or isinstance(b, Fact)):
        return a is b or (
            isinstance(a, Fact) and isinstance(b, Fact) and
            fact_uid(a) is not None and fact_uid(a) == fact_uid(b)
        )
    try:
        return bool(a == b)
    except Exception:
        return False


def head_binding(elem_args: Sequence, match: Sequence,
                 skip_derefs=False) -> Optional[dict]:
    ''' Unify an element's head args with a TaskEx's match values. Returns
        a binding dict, or None if they don't unify. With skip_derefs, deref
        args (e.g. `x.on`) are ignored instead of raising.
    '''
    if(len(elem_args) != len(match)):
        return None
    binding = {}
    for x, val in zip(elem_args, match):
        binding = _unify(x, val, binding, skip_derefs)
        if(binding is None):
            return None
    return binding


# ------------------------------------------------------
# : Facts across states

def fact_uid(fact: Fact):
    ''' The unique id of a fact, or None if its type declares none. '''
    return fact.get_unique_id()


def _fact_by_uid(fs: FactSet, uid) -> Optional[Fact]:
    try:
        return fs.get_fact(unique_id=uid)
    except (KeyError, TypeError, ValueError):
        return None


def _resolve_fact(fs: FactSet, fact: Fact) -> Optional[Fact]:
    ''' Find `fact` in `fs`. A binding can hold a Fact from an earlier
        state's FactSet, so re-resolve it by unique id. Returns None if the
        fact isn't present (e.g. it was removed from the state).
    '''
    uid = fact_uid(fact)
    if(uid is not None):
        return _fact_by_uid(fs, uid)
    for f in fs:
        if(f is fact):
            return f
    return None


def _ground_value(var: Var, val, fs: FactSet):
    ''' Resolve a bound value to a Fact in `fs` where it should be one:
        a Fact (possibly from an older FactSet) is re-resolved by unique id,
        and a plain value bound to a fact-typed var is looked up as a unique
        id. Returns (ok, value); ok is False if the fact isn't in `fs`.
    '''
    if(isinstance(val, Fact)):
        val = _resolve_fact(fs, val)
    elif(is_fact_typed(var) and not is_var(val)):
        val = _fact_by_uid(fs, val)
    else:
        return True, val
    return val is not None, val


def ground_head(elem_args: Sequence, binding: dict, fs: FactSet) -> Optional[dict]:
    ''' Ground the head vars of a binding against `fs` (see _ground_value).
        Returns None if a fact the head refers to isn't in the state.
    '''
    out = dict(binding)
    for x in elem_args:
        if(is_var(x) and not is_deref(x)):
            a = alias_of(x)
            if(a in out):
                ok, out[a] = _ground_value(x, out[a], fs)
                if(not ok):
                    return None
    return out


# ------------------------------------------------------
# : Matching

def iter_bindings(logic: Optional[Logic], fs: FactSet,
                  binding: dict) -> Iterator[dict]:
    ''' Yield every extension of `binding` that satisfies `logic` in `fs`.
        With no logic, yields `binding` itself.
    '''
    if(logic is None):
        yield binding
        return

    # cre rejects partials that name vars the Logic doesn't have, so
    #  only pin the vars the Logic uses and merge the rest back after.
    used = {alias_of(v): v for v in logic.get_vars()}
    partial = {}
    for a, val in binding.items():
        if(a not in used or is_var(val)):
            continue
        ok, val = _ground_value(used[a], val, fs)
        # A pinned fact that isn't in the state can't match.
        if(not ok):
            return
        partial[a] = val

    for m in logic.get_matches(fs, partial=partial or None):
        yield {**binding, **dict(m.items())}


def any_match(logic: Optional[Logic], fs: FactSet, binding: dict) -> bool:
    for _ in iter_bindings(logic, fs, binding):
        return True
    return False


def resolve(x, binding: dict):
    ''' Substitute bound values into an arg (Var, deref, constant, or a
        tuple of these). Unbound vars are left as Vars.
    '''
    if(is_var(x)):
        a = alias_of(x)
        if(a not in binding):
            return x
        val = binding[a]
        if(is_deref(x)):
            return x(val)
        return val
    if(isinstance(x, tuple)):
        return tuple(resolve(xi, binding) for xi in x)
    return x


def resolve_all(args: Sequence, binding: dict) -> tuple:
    return tuple(resolve(x, binding) for x in args)
