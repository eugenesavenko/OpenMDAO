"""
OpenMDAO Wrapper for pyoptsparse.
pyoptsparse is based on pyOpt, which is an object-oriented framework for
formulating and solving nonlinear constrained optimization problems, with
additional MPI capability. Note: only SNOPT is supported right now.
"""

from __future__ import print_function

from six import iterkeys, iteritems
import numpy as np

from pyoptsparse import Optimization

from openmdao.core.driver import Driver
from openmdao.util.record_util import create_local_meta, update_local_meta

class pyOptSparseDriver(Driver):
    """ Driver wrapper for pyoptsparse. pyoptsparse is based on pyOpt, which
    is an object-oriented framework for formulating and solving nonlinear
    constrained optimization problems, with additional MPI capability. Note:
    only SNOPT is supported right now.

    Options
    -------
    equality_constraints :  bool(True)
    inequality_constraints :  bool(True)
    integer_parameters :  bool(False)
    linear_constraints :  bool(False)
    multiple_objectives :  bool(False)
    two_sided_constraints :  bool(True)
    exit_flag :  int(0)
        0 for fail, 1 for ok
    optimizer :  str('SNOPT')
        Name of optimizers to use
    print_results :  bool(True)
        Print pyOpt results if True
    pyopt_diff :  bool(True)
        Set to True to let pyOpt calculate the gradient
    title :  str('Optimization using pyOpt_sparse')
        Title of this optimization run

    """

    def __init__(self):
        """Initialize pyopt"""

        super(pyOptSparseDriver, self).__init__()

        # What we support
        self.supports['inequality_constraints'] = True
        self.supports['equality_constraints'] = True
        self.supports['multiple_objectives'] = False
        self.supports['two_sided_constraints'] = True

        # TODO: Support these
        self.supports['linear_constraints'] = False
        self.supports['integer_parameters'] = False

        # User Options
        self.options.add_option('optimizer', 'SNOPT', values=['SNOPT'],
                                desc='Name of optimizers to use')
        self.options.add_option('title', 'Optimization using pyOpt_sparse',
                                desc='Title of this optimization run')
        self.options.add_option('print_results', True,
                                desc='Print pyOpt results if True')
        self.options.add_option('pyopt_diff', False,
                                desc='Set to True to let pyOpt calculate the gradient')
        self.options.add_option('exit_flag', 0,
                                desc='0 for fail, 1 for ok')

        # The user places optimizer-specific settings in here.
        self.opt_settings = {}

        self.pyopt_solution = None

        self.lin_jacs = {}
        self.quantities = []
        self.metadata = None
        self.exit_flag = 0
        self._problem = None

    def run(self, problem):
        """pyOpt execution. Note that pyOpt controls the execution, and the
        individual optimizers (i.e., SNOPT) control the iteration.

        Args
        ----
        problem : `Problem`
            Our parent `Problem`.
        """

        self.pyopt_solution = None
        rel = problem.root._relevance

        # Metadata Setup
        self.metadata = create_local_meta(None, self.options['optimizer'])
        self.iter_count = 0
        update_local_meta(self.metadata, (self.iter_count,))

        # Initial Run
        problem.root.solve_nonlinear(metadata=self.metadata)

        opt_prob = Optimization(self.options['title'], self.objfunc)

        # Add all parameters
        param_meta = self.get_param_metadata()
        self.param_list = param_list = list(iterkeys(param_meta))
        param_vals = self.get_params()
        for name, meta in iteritems(param_meta):
            opt_prob.addVarGroup(name, meta['size'], type='c',
                                 value=param_vals[name],
                                 lower=meta['low'], upper=meta['high'])

        opt_prob.finalizeDesignVariables()

        # Add all objectives
        objs = self.get_objectives()
        self.quantities = list(iterkeys(objs))
        for name in objs:
            opt_prob.addObj(name)

        # Calculate and save gradient for any linear constraints.
        lcons = self.get_constraints(lintype='linear').values()
        if len(lcons) > 0:
            self.lin_jacs = problem.calc_gradient(param_list, lcons,
                                                  return_format='dict')
            #print("Linear Gradient")
            #print(self.lin_jacs)

        # Add all equality constraints
        econs = self.get_constraints(ctype='eq', lintype='nonlinear')
        con_meta = self.get_constraint_metadata()
        self.quantities += list(iterkeys(econs))
        for name in econs:
            size = con_meta[name]['size']
            lower = upper = con_meta[name]['equals']

            # Sparsify Jacobian via relevance
            wrt = rel.relevant[name].intersection(param_list)

            if con_meta[name]['linear'] is True:
                opt_prob.addConGroup(name, size, lower=lower, upper=upper,
                                     linear=True, wrt=wrt,
                                     jac=self.lin_jacs[name])
            else:
                opt_prob.addConGroup(name, size, lower=lower, upper=upper,
                                     wrt=wrt)

        # Add all inequality constraints
        incons = self.get_constraints(ctype='ineq', lintype='nonlinear')
        self.quantities += list(iterkeys(incons))
        for name in incons:
            size = con_meta[name]['size']

            # Bounds - double sided is supported
            lower = con_meta[name]['lower']
            upper = con_meta[name]['upper']

            # Sparsify Jacobian via relevance
            wrt = rel.relevant[name].intersection(param_list)

            if con_meta[name]['linear'] is True:
                opt_prob.addConGroup(name, size, upper=upper, lower=lower,
                                     linear=True, wrt=wrt,
                                     jac=self.lin_jacs[name])
            else:
                opt_prob.addConGroup(name, size, upper=upper, lower=lower,
                                     wrt=wrt)

        # Instantiate the requested optimizer
        optimizer = self.options['optimizer']
        try:
            exec('from pyoptsparse import %s' % optimizer)
        except ImportError:
            msg = "Optimizer %s is not available in this installation." % \
                   optimizer
            raise ImportError(msg)

        optname = vars()[optimizer]
        opt = optname()

        #Set optimization options
        for option, value in self.opt_settings.items():
            opt.setOption(option, value)

        self._problem = problem

        # Execute the optimization problem
        if self.options['pyopt_diff'] is True:
            # Use pyOpt's internal finite difference
            fd_step = problem.root.fd_options['step_size']
            sol = opt(opt_prob, sens='FD', sensStep=fd_step)
        else:
            # Use OpenMDAO's differentiator for the gradient
            sol = opt(opt_prob, sens=self.gradfunc)

        self._problem = None

        # Print results
        if self.options['print_results'] is True:
            print(sol)

        # Pull optimal parameters back into framework and re-run, so that
        # framework is left in the right final state
        dv_dict = sol.getDVs()
        for name in param_list:
            val = dv_dict[name]
            self.set_param(name, val)

        self.root.solve_nonlinear(metadata=self.metadata)

        # Save the most recent solution.
        self.pyopt_solution = sol
        try:
            exit_status = sol.optInform['value']
            self.exit_flag = 1
            if exit_status > 2: # bad
                self.exit_flag = 0
        except KeyError: #nothing is here, so something bad happened!
            self.exit_flag = 0

    def objfunc(self, dv_dict):
        """ Function that evaluates and returns the objective function and
        constraints. This function is passed to pyOpt's Optimization object
        and is called from its optimizers.

        Args
        ----
        dv_dict : dict
            Dictionary of design variable values.

        Returns
        -------
        func_dict : dict
            Dictionary of all functional variables evaluated at design point.
        fail : int
            0 for successful function evaluation
            1 for unsuccessful function evaluation
        """

        fail = 1
        func_dict = {}
        metadata = self.metadata
        system = self.root
        comm = system.comm
        iproc = comm.rank
        nproc = comm.size

        try:
            for name in self.param_list:
                self.set_param(name, dv_dict[name])

            # Execute the model
            #print("Setting DV")
            #print(dv_dict)

            self.iter_count += 1
            update_local_meta(metadata, (self.iter_count,))

            system.solve_nonlinear(metadata=metadata)

            # Get the objective function evaluations
            for name, obj in iteritems(self.get_objectives()):
                # if nproc > 1:
                #     owner = system._owning_ranks[name]
                #     if iproc == owner:
                #         func_dict[name] = comm.bcast(obj, root=owner)
                #     else:
                #         func_dict[name] = comm.bcast(None, root=owner)
                # else:
                func_dict[name] = obj

            # Get the constraint evaluations
            for name, con in iteritems(self.get_constraints()):
                # if nproc > 1:
                #     owner = system._owning_ranks[name]
                #     if iproc == owner:
                #         func_dict[name] = comm.bcast(con, root=owner)
                #     else:
                #         func_dict[name] = comm.bcast(None, root=owner)
                # else:
                func_dict[name] = con

            # Record after getting obj and constraint to assure they have
            # been gathered in MPI.
            self.record(metadata)

            # Get the double-sided constraint evaluations
            #for key, con in iteritems(self.get_2sided_constraints()):
            #    func_dict[name] = np.array(con.evaluate(self.parent))

            fail = 0

        except Exception as msg:

            # Exceptions seem to be swallowed by the C code, so this
            # should give the user more info than the dreaded "segfault"
            print("Exception: %s" % str(msg))
            print(70*"=")
            import traceback
            traceback.print_exc()
            print(70*"=")
            fail = 1

        #print("Functions calculated")
        #print(func_dict)
        return func_dict, fail

    def gradfunc(self, dv_dict, func_dict):
        """ Function that evaluates and returns the gradient of the objective
        function and constraints. This function is passed to pyOpt's
        Optimization object and is called from its optimizers.

        Args
        ----
        dv_dict : dict
            Dictionary of design variable values.
        func_dict : dict
            Dictionary of all functional variables evaluated at design point.

        Returns
        -------
        sens_dict : dict
            Dictionary of dictionaries for gradient of each dv/func pair
        fail : int
            0 for successful function evaluation
            1 for unsuccessful function evaluation
        """

        fail = 1
        sens_dict = {}

        try:
            sens_dict = self._problem.calc_gradient(dv_dict.keys(),
                                                    self.quantities,
                                                    return_format='dict')
            #for key, value in iteritems(self.lin_jacs):
            #    sens_dict[key] = value

            fail = 0

        except Exception as msg:

            # Exceptions seem to be swallowed by the C code, so this
            # should give the user more info than the dreaded "segfault"
            print("Exception: %s" % str(msg))
            print(70*"=")
            import traceback
            traceback.print_exc()
            print(70*"=")

        #print("Derivatives calculated")
        #print(dv_dict)
        #print(sens_dict)
        return sens_dict, fail
