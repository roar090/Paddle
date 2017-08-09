import unittest

import numpy
import paddle.v2.framework.core as core
from paddle.v2.framework.op import Operator

__all__ = ['get_numeric_gradient']


def create_op(op_type):
    kwargs = dict()
    for in_name in Operator.get_op_input_names(op_type):
        kwargs[in_name] = in_name
    for out_name in Operator.get_op_output_names(op_type):
        kwargs[out_name] = out_name

    return Operator(op_type, **kwargs)


def grad_var_name(var_name):
    return var_name + "@GRAD"


def get_numeric_gradient(op,
                         input_values,
                         output_name,
                         input_to_check,
                         delta=0.005,
                         local_scope=None):
    """
    Get Numeric Gradient for an operator's input.
    
    :param op: C++ operator instance, could be an network 
    :param input_values: The input variables. Should be an dictionary, key is 
    variable name. Value is numpy array.
    :param output_name: The final output variable name. 
    :param input_to_check: The input variable need to get gradient.
    :param delta: The perturbation value for numeric gradient method. The 
    smaller delta is, the more accurate result will get. But if that delta is
     too small, it could occur numerical stability problem.
    :param local_scope: The local scope used for get_numeric_gradient.
    :return: The gradient array in numpy format.
    """
    if local_scope is None:
        local_scope = core.Scope()

    # Create all input variable in local_scope
    for var_name in input_values:
        var = local_scope.new_var(var_name)
        tensor = var.get_tensor()
        tensor.set_dims(input_values[var_name].shape)
        tensor.alloc_float(core.CPUPlace())
        tensor.set(input_values[var_name], core.CPUPlace())

    # Create all output variable in local_scope
    for output in op.outputs():
        if local_scope.find_var(output) is None:
            local_scope.new_var(output).get_tensor()

    op.infer_shape(local_scope)

    # allocate output memory
    for output in op.outputs():
        local_scope.find_var(output).get_tensor().alloc_float(core.CPUPlace())

    # TODO(yuyang18): Only CPU is support now.
    cpu_ctx = core.DeviceContext.create(core.CPUPlace())

    def get_output():
        op.run(local_scope, cpu_ctx)
        return numpy.array(local_scope.find_var(output_name).get_tensor()).sum()

    def product(dim):
        return reduce(lambda a, b: a * b, dim, 1)

    tensor_to_check = local_scope.find_var(input_to_check).get_tensor()
    tensor_size = product(tensor_to_check.get_dims())
    gradient_flat = numpy.zeros(shape=(tensor_size, ), dtype='float32')
    for i in xrange(tensor_size):
        origin = tensor_to_check.get_float_element(i)
        x_pos = origin + delta
        tensor_to_check.set_float_element(i, x_pos)
        y_pos = get_output()

        x_neg = origin - delta
        tensor_to_check.set_float_element(i, x_neg)
        y_neg = get_output()

        tensor_to_check.set_float_element(i, origin)  # restore old value
        gradient_flat[i] = (y_pos - y_neg) / delta / 2
    return gradient_flat.reshape(tensor_to_check.get_dims())


class GradientChecker(unittest.TestCase):
    def __is_close(self, numeric_grads, scope, max_relative_error):
        for name in numeric_grads:
            op_grad = numpy.array(
                scope.find_var(grad_var_name(name)).get_tensor())
            is_close = numpy.allclose(
                numeric_grads[name], op_grad, rtol=max_relative_error, atol=100)
            if not is_close:
                return False
        return True

    def check_grad(self,
                   forward_op,
                   input_vars,
                   inputs_to_check,
                   output_name,
                   no_grad_set=None,
                   only_cpu=False,
                   max_relative_error=0.005):
        """
        :param forward_op: used to create backward_op
        :param input_vars: numpy value of input variable. The following
            computation will use these variables.
        :param inputs_to_check: inputs var names that should check gradient.
        :param output_name: output name that used to
        :param max_relative_error: The relative tolerance parameter.
        :param no_grad_set: used when create backward ops
        :param only_cpu: only compute and check gradient on cpu kernel.
        :return:
        """
        if no_grad_set is None:
            no_grad_set = set()

        tmp_outs = forward_op.temp_outputs()
        no_tmp_out = filter(lambda name: name not in tmp_outs,
                            forward_op.outputs())
        if len(no_tmp_out) != 1:
            raise ValueError("non temp out_names should be 1")

        in_names = forward_op.inputs()
        for no_grad in no_grad_set:
            if no_grad not in in_names:
                raise ValueError("no_grad should be in in_names")

        backward_op = core.Operator.backward(forward_op, no_grad_set)

        places = [core.CPUPlace()]
        if not only_cpu and core.is_compile_gpu() and backward_op.support_gpu():
            places.append(core.GPUPlace(0))

        numeric_grad = dict()
        # get numeric gradient
        for check_name in inputs_to_check:
            numeric_grad[check_name] = \
                get_numeric_gradient(forward_op, input_vars, output_name, check_name)

        # get operator gradient according to different device
        for place in places:
            scope = core.Scope()
            ctx = core.DeviceContext.create(place)

            # create input var and set value
            for name, value in input_vars.iteritems():
                if name not in in_names:
                    raise ValueError(name + " not in op.inputs_")
                var = scope.new_var(name).get_tensor()
                var.set_dims(value.shape)
                var.set(value, place)

            # create output var
            for out_name in forward_op.outputs():
                scope.new_var(out_name).get_tensor()

            # infer the shape of output var and compute/set value of output var
            forward_op.infer_shape(scope)
            forward_op.run(scope, ctx)

            # create output grad var
            # set shape as the output var
            # set value of this grad to ones
            for name in forward_op.outputs():
                out_tensor = scope.find_var(name).get_tensor()
                grad_tensor = scope.new_var(grad_var_name(name)).get_tensor()
                grad_tensor.set_dims(out_tensor.shape())
                data = 1.0 * numpy.ones(out_tensor.shape())
                grad_tensor.set(data, place)

            # create input grad var
            for name in backward_op.outputs():
                scope.new_var(name).get_tensor()

            # infer the shape of input gradient var and compute/set it's value
            # with backward op
            backward_op.infer_shape(scope)
            backward_op.run(scope, ctx)

            if isinstance(place, core.CPUPlace):
                msg = "CPU kernel gradient is not close to numeric gradient"
            else:
                if isinstance(place, core.GPUPlace):
                    msg = "GPU kernel gradient is not close to numeric gradient"
                else:
                    raise ValueError("unknown place " + type(place))
            self.assertTrue(
                self.__is_close(numeric_grad, scope, max_relative_error), msg)


if __name__ == '__main__':

    class GetNumericGradientTest(unittest.TestCase):
        def test_add_op(self):
            add_op = Operator('add_two', X="X", Y="Y", Out="Z")
            x = numpy.random.random((10, 1)).astype("float32")
            y = numpy.random.random((10, 1)).astype("float32")

            arr = get_numeric_gradient(add_op, {'X': x, "Y": y}, 'Z', 'X')
            self.assertAlmostEqual(arr.mean(), 1.0, delta=1e-2)

        def test_softmax_op(self):
            def stable_softmax(x):
                """Compute the softmax of vector x in a numerically stable way."""
                shiftx = x - numpy.max(x)
                exps = numpy.exp(shiftx)
                return exps / numpy.sum(exps)

            def label_softmax_grad(Y, dY):
                dX = Y * 0.0
                for i in range(Y.shape[0]):
                    d = numpy.dot(Y[i, :], dY[i, :])
                    dX[i, :] = Y[i, :] * (dY[i, :] - d)
                return dX

            softmax_op = Operator("softmax", X="X", Y="Y")

            X = numpy.random.random((2, 2)).astype("float32")
            Y = numpy.apply_along_axis(stable_softmax, 1, X)
            dY = numpy.ones(Y.shape)
            dX = label_softmax_grad(Y, dY)

            arr = get_numeric_gradient(softmax_op, {"X": X}, 'Y', 'X')
            numpy.testing.assert_almost_equal(arr, dX, decimal=1e-2)

    unittest.main()