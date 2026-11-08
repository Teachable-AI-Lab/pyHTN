from pyhtn.htn import HTN, Method, Operator, Task, MethodEx, OperatorEx, TaskEx, Unord
from pyhtn.htn.element_executions import tree_dict_to_str
from pyhtn.htn import HTN_Planner
from cre import Var, FactType
from unittest.mock import MagicMock
from pprint import pprint


# A single fact type holds the boolean flags each test's state sets.
Flags = FactType("Flags", {
    "id": {"type": str, "unique_id": True},
    "skip_A": bool, "skip_B": bool, "opt_C": bool, "opt_D": bool,
    "opt_a": bool, "opt_b": bool, "opt_F": bool, "opt_x": bool, "opt_y": bool,
    "skip_a": bool, "skip_b": bool, "skip_F": bool, "skip_x": bool, "skip_y": bool,
    "start": bool, "rewrite_and_multiply": bool, "equation": str,
})
F = Var(Flags, "F")

operators = [Operator(name) for name in "abcdefgxyz"]


def setup(domain, state=[{"type": "Flags", "id": "1"}], args=[]):
    env = MagicMock()
    env.get_state.return_value = state
    planner = HTN_Planner(
        tasks = [{'name': 'S', 'args': args}],
        domain = domain,
        env = env,
        enable_logging=True
    )
    return planner

def plan_next(planner):
    trace = planner.plan_to_next_operators(multiheaded=True)
    # print(trace.print_trace())
    planner.print_current_frames()
    print()

def check_next(planner, op_names, nxt_op):
    operator_execs = planner.get_next_operator_execs()
    res_op_names = [op_ex.operator.name for op_ex in operator_execs]
    assert res_op_names == op_names, f"{res_op_names} != {op_names}"
    planner.apply(operator_execs[op_names.index(nxt_op)])
    return operator_execs

def check_exhausted(planner):
    assert planner.is_exhausted()
    planner.print_current_frames()


domain0 = HTN(
    *operators,

    Method('S',
        subtasks=[
            Task("a", optional=True),
            Unord(
                Task('b', optional=True),
                Task('c', optional=True),
                Task('d', optional=True),
            ),
            Unord(
                Task('e', optional=True),
                Task('f'),
            ),
            Task('g'),
        ],
    ),
)

def test_basic_unord_skip():
    print("--------------")
    planner = setup(domain0)
    plan_next(planner)
    check_next(planner, ['a', 'b', 'c', 'd', 'e', 'f'], 'd')
    plan_next(planner)
    check_next(planner, ['b', 'c', 'e', 'f'], 'f')
    plan_next(planner)
    check_next(planner, ['e', 'g'], 'g')
    check_exhausted(planner)


# The older dict format still works; dict keys name the tasks.
domain0_dict = {
    **{op.name : [op] for op in operators},
    "S" : domain0["S"],
}

def test_dict_domain():
    planner = setup(domain0_dict)
    plan_next(planner)
    check_next(planner, ['a', 'b', 'c', 'd', 'e', 'f'], 'd')
    plan_next(planner)
    check_next(planner, ['b', 'c', 'e', 'f'], 'f')
    plan_next(planner)
    check_next(planner, ['e', 'g'], 'g')
    check_exhausted(planner)


domain1 = HTN(
    *operators,

    Method('S',
        subtasks=[
            Task("U"),
            Task('f', optional=True),
            Task('g'),
        ],
    ),

    Method('U',
        subtasks=[
            Task('a', optional=True),
            Task('Q'),
            Task('B'),
            Unord(
                Task('c'),
                Task('d'),
                Task('e', optional=True),
            ),
        ],
    ),

# Q(A,B,C)
# MethodQ(A,B=None,C=None)
# MethodQ(A)
# Q(1)

    # Tests that committing to a method narrows executable operators
    #   to the scope of that method (and its descendants).
    Method('Q', subtasks=[Task('b'), Task('T')]),

    # Test that skippabilitiy propogates up multiple levels
    Method('Q', subtasks=[Task('R')]),

    Method('R', subtasks=[]),
    Method('T', subtasks=[Task("z")]),

    # Tests that skipped optionals propogate effective span into the parent
    Method('B', subtasks=[Task('x'), Task('y', optional=True)]),
    Method('B', subtasks=[]),
)

def test_heir_unord_skip():
    print("--------------")
    planner = setup(domain1)
    plan_next(planner)
    check_next(planner, ['a', 'b', 'x', 'c', 'd', 'e'], 'b')
    plan_next(planner)
    check_next(planner, ['z'], 'z')
    plan_next(planner)
    check_next(planner, ['x', 'c', 'd', 'e'], 'x')
    plan_next(planner)
    check_next(planner, ['y', 'c', 'd', 'e'], 'y')
    plan_next(planner)
    check_next(planner, ['c', 'd', 'e'], 'd')
    plan_next(planner)
    check_next(planner, ['c', 'e'], 'c')
    plan_next(planner)
    check_next(planner, ['e', 'f', 'g'], 'f')
    plan_next(planner)
    check_next(planner, ['g'], 'g')
    check_exhausted(planner)


domain2 = HTN(
    *operators,

    Method('S',
        subtasks=[
            Task("A"),
            Task('B'),
            Task('C'),
            Task('D'),
            Task('e'),
        ],
    ),
    Method('A', pre=F.skip_A == False, subtasks=[Task('a')]),
    Method('A', pre=F.skip_A == True, subtasks=[]),

    Method('B', pre=F.skip_B == False,
        subtasks=Unord(Task('z',optional=True), Task('b'))),
    Method('B', pre=F.skip_B == True, subtasks=[]),

    Method('C', subtasks=[Task('c')]),
    Method('C', pre=F.opt_C == True, subtasks=[]),

    Method('D', subtasks=[Task('d')]),
    Method('D', pre=F.opt_D == True, subtasks=[]),
)

def test_conditional_empty_methods():
    print("--------------")
    state = [{
        'type': 'Flags', 'id': '1',
        'skip_A': True, "skip_B" : False,
        'opt_C': True, "opt_D" : False,
    }]
    planner = setup(domain2, state)
    plan_next(planner)
    check_next(planner, ['z','b'], 'b')
    plan_next(planner)
    check_next(planner, ['z', 'c','d'], 'd')
    plan_next(planner)
    check_next(planner, ['e'], 'e')
    check_exhausted(planner)


domain3 = HTN(
    *operators,

    Method('S',
        subtasks=[
            Task('a', optional_if=F.opt_a == True),
            Task('b', optional_if=F.opt_b == True),
            Task('D'),
            Task('e'),
            Task('F', optional_if=F.opt_F == True),
            Task('g'),
        ],
    ),
    Method('D', subtasks=[
        Task('x', optional_if=F.opt_x == True),
        Task('y', optional_if=F.opt_y == True),
    ]),
    Method('D', subtasks=[Task('z')]),
    Method('F', subtasks=[Task('f')]),
)

def test_optional_if():
    state = [{
        'type': 'Flags', 'id' : "1",
        'opt_a': True, "opt_b" : False,
        'opt_F': True,
        "opt_x" : False, "opt_y" : True,
    }]
    planner = setup(domain3, state)
    plan_next(planner)
    check_next(planner, ['a','b'], 'b')
    plan_next(planner)
    check_next(planner, ['z', 'x'], 'x') # TODO: out of order
    plan_next(planner)
    check_next(planner, ['y', 'e'], 'e')
    plan_next(planner)
    check_next(planner, ['f', 'g'], 'f')
    plan_next(planner)
    check_next(planner, ['g'], 'g')
    check_exhausted(planner)


domain4 = HTN(
    *operators,

    Method('S',
        subtasks=[
            Task('a', skip_if=F.skip_a == True),
            Task('b', skip_if=F.skip_b == True),
            Task('D'),
            Task('e'),
            Task('F', skip_if=F.skip_F == True),
            Task('g'),
        ],
    ),
    Method('D', subtasks=[
        Task('x', skip_if=F.skip_x == True),
        Task('y', skip_if=F.skip_y == True),
    ]),
    Method('D', subtasks=[Task('z')]),
    Method('F', subtasks=[Task('f')]),
)

def test_skip_if():
    state = [{
        'type': 'Flags', 'id' : "1",
        'skip_a': True, "skip_b" : False,
        'skip_F': True,
        "skip_x" : False, "skip_y" : True,
    }]
    planner = setup(domain4, state)
    plan_next(planner)
    check_next(planner, ['b'], 'b')
    plan_next(planner)
    check_next(planner, ['z', 'x'], 'x') # TODO: out of order
    plan_next(planner)
    check_next(planner, ['e'], 'e')
    plan_next(planner)
    check_next(planner, ['g'], 'g')
    check_exhausted(planner)


eq, kc = Var("equation"), Var("kc")

domain_al = HTN(
    Operator('done', kc),
    Operator('rewrite_using_rule', eq, kc),
    Operator('multiply_exponents', eq, kc),
    Operator('solve_using_power_rule', eq, kc),

    Method('rewrite_and_multiply', eq,
        subtasks=Unord(
            Task('rewrite_using_rule', eq, ('rewrite_using_rule',)),
            Task('multiply_exponents', eq, ('multiply_exponents',)),
        ),
    ),

    Method('S', eq,
        subtasks=[
            Unord(
                Task('rewrite_and_multiply', eq,
                     skip_if=F.rewrite_and_multiply == False),
                Task('solve_using_power_rule', eq, ('solve_using_power_rule',)),
            ),
            Task('done', ('done',)),
        ],
    ),
)

def test_al_domain():
    state = [{
        'type': 'Flags', 'id' : "1",
        'start' : True,
        'rewrite_and_multiply': True,
        'equation' : '1+1'
    }]
    planner = setup(domain_al, state, ["1+1"])
    plan_next(planner)
    check_next(planner, ['rewrite_using_rule', 'multiply_exponents', 'solve_using_power_rule'], 'multiply_exponents')
    plan_next(planner)
    check_next(planner, ['rewrite_using_rule'], 'rewrite_using_rule')
    plan_next(planner)
    check_next(planner, ['solve_using_power_rule'], 'solve_using_power_rule')
    plan_next(planner)
    check_next(planner, ['done'], 'done')
    check_exhausted(planner)

    planner = setup(domain_al, state, ["1+1"])
    plan_next(planner)
    check_next(planner, ['rewrite_using_rule', 'multiply_exponents', 'solve_using_power_rule'], 'rewrite_using_rule')
    plan_next(planner)
    check_next(planner, ['multiply_exponents'], 'multiply_exponents')
    plan_next(planner)
    check_next(planner, ['solve_using_power_rule'], 'solve_using_power_rule')
    plan_next(planner)
    check_next(planner, ['done'], 'done')
    check_exhausted(planner)

    state = [{
        'type': 'Flags', 'id' : "1",
        'start' : True,
        'rewrite_and_multiply': False,
        'equation' : '1+1'
    }]
    planner = setup(domain_al, state, ["1+1"])
    plan_next(planner)
    check_next(planner, ['solve_using_power_rule'], 'solve_using_power_rule')
    plan_next(planner)
    check_next(planner, ['done'], 'done')
    check_exhausted(planner)



if __name__ == "__main__":
    test_basic_unord_skip()
    test_dict_domain()
    test_heir_unord_skip()
    test_conditional_empty_methods()
    test_optional_if()
    test_skip_if()
    test_al_domain()
