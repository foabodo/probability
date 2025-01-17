# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Structural Time Series base class."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections

# Dependency imports
import tensorflow.compat.v2 as tf

from tensorflow_probability.python import util as tfp_util
from tensorflow_probability.python.internal import distribution_util
from tensorflow_probability.python.sts.internal import util as sts_util

tfl = tf.linalg

Parameter = collections.namedtuple('Parameter', ['name', 'prior', 'bijector'])


class StructuralTimeSeries(object):
  """Base class for structural time series models.

  A StructuralTimeSeries object represents a declarative specification of a
  structural time series model, including priors on model parameters.
  It implements a joint probability model
    `p(params, y) = p(params) p(y | params)`,
  where `params` denotes a list of real-valued parameters specified by the child
  class, and `p(y | params)` is a linear Gaussian state space model with
  structure determined by the child class.
  """

  def __init__(self, parameters, latent_size, init_parameters=None,
               name='StructuralTimeSeries'):
    """Construct a specification for a structural time series model.

    Args:
      parameters: list of Parameter namedtuples, each specifying the name
        and prior distribution of a model parameter along with a
        bijective transformation from an unconstrained space to the support
        of that parameter. The order of this list determines the canonical
        parameter ordering used by fitting and inference algorithms.
      latent_size: Python `int` specifying the dimensionality of the latent
        state space for this model.
      init_parameters: Python `dict` of parameters used to instantiate this
        model component.
      name: Python `str` name for this model component.
    """

    self._init_parameters = init_parameters
    self._parameters = parameters
    self._latent_size = latent_size
    self._name = name

  @property
  def init_parameters(self):
    """Parameters used to instantiate this `StructuralTimeSeries`."""
    if self._init_parameters is None:
      raise ValueError(
          'Component has `init_parameters` of `None`. See the built-in '
          'components (e.g., `LocalLevel`) for examples of how to properly '
          'store parameters to `__init__`.')
    return {k: v for k, v in self._init_parameters.items()
            if not k.startswith('__') and v is not self}

  @property
  def parameters(self):
    """List of Parameter(name, prior, bijector) namedtuples for this model."""
    return self._parameters

  @property
  def latent_size(self):
    """Python `int` dimensionality of the latent space in this model."""
    return self._latent_size

  @property
  def name(self):
    """Name of this model component."""
    return self._name

  @property
  def batch_shape(self):
    """Static batch shape of models represented by this component.

    Returns:
      batch_shape: A `tf.TensorShape` giving the broadcast batch shape of
        all model parameters. This should match the batch shape of
        derived state space models, i.e.,
        `self.make_state_space_model(...).batch_shape`. It may be partially
        defined or unknown.
    """
    batch_shape = tf.TensorShape([])
    for param in self.parameters:
      batch_shape = tf.broadcast_static_shape(
          batch_shape, param.prior.batch_shape)
    return batch_shape

  def batch_shape_tensor(self):
    """Runtime batch shape of models represented by this component.

    Returns:
      batch_shape: `int` `Tensor` giving the broadcast batch shape of
        all model parameters. This should match the batch shape of
        derived state space models, i.e.,
        `self.make_state_space_model(...).batch_shape_tensor()`.
    """
    batch_shape = tf.constant([], dtype=tf.int32)
    for param in self.parameters:
      batch_shape = tf.broadcast_dynamic_shape(
          batch_shape, param.prior.batch_shape_tensor())
    return batch_shape

  def __add__(self, other):
    """Models the sum of the series from the two components."""
    # Local import to avoid circular dependency.
    from tensorflow_probability.python.sts.components import sum as sts_sum  # pylint: disable=g-import-not-at-top

    sum_kwargs = {}
    if isinstance(self, sts_sum.Sum) and isinstance(other, sts_sum.Sum):
      # Debatably, a Sum + Sum should sum the `constant_offset` parameters, and
      # should model the sum of the component observation noises (e.g., if
      # `noise1 ~ N(0, scale=1)` and `noise2 ~ N(0, scale=1)`, then
      # `noise1 + noise2 ~ N(0, scale=sqrt(2))`). But this would be surprising
      # in the likely-common case where both sums are modeling the same
      # observed_time_series. So we instead treat Sum + Sum as a logical
      # 'merge' operation on the components, requiring that all other parameters
      # match.
      _assert_dict_contents_are_equal(
          self.init_parameters, other.init_parameters,
          ignore=['components'],
          message='Cannot add Sum components with different parameter values.')
      sum_kwargs = self.init_parameters
    elif isinstance(self, sts_sum.Sum):
      sum_kwargs = self.init_parameters
    elif isinstance(other, sts_sum.Sum):
      sum_kwargs = other.init_parameters
    else:
      # If creating a Sum from scratch, try to infer a heuristic noise prior.
      observed_time_series = self.init_parameters.get(
          'observed_time_series', None)
      if observed_time_series is None:
        observed_time_series = other.init_parameters.get(
            'observed_time_series', None)
      if observed_time_series is None:
        raise ValueError('Could not automatically create a `Sum` component '
                         'because neither summand was initialized with an '
                         '`observed_time_series`. You may still instantiate '
                         'the component manually as `tfp.sts.Sum(...).')
      sum_kwargs['observed_time_series'] = observed_time_series

    my_components = getattr(self, 'components', [self])
    other_components = getattr(other, 'components', [other])
    sum_kwargs['components'] = list(my_components) + list(other_components)
    return sts_sum.Sum(**sum_kwargs)

  def copy(self, **override_parameters_kwargs):
    """Creates a deep copy.

    Note: the copy distribution may continue to depend on the original
    initialization arguments.

    Args:
      **override_parameters_kwargs: String/value dictionary of initialization
        arguments to override with new values.
    Returns:
      copy: A new instance of `type(self)` initialized from the union
        of self.init_parameters and override_parameters_kwargs, i.e.,
        `dict(self.init_parameters, **override_parameters_kwargs)`.
    """
    parameters = dict(self.init_parameters, **override_parameters_kwargs)
    return type(self)(**parameters)

  def _canonicalize_param_vals_as_map(self, param_vals):
    """If given an ordered list of parameter values, build a name:value map.

    This is a utility method that allows parameter values to be specified as
    either lists or dicts, by transforming lists to a canonical dict
    representation.

    Args:
      param_vals: Python list (or other `iterable`) of `Tensor` values
        corresponding to the parameters listed in `self.parameters`,
        OR a map (Python `dict`) of parameter names to values.

    Returns:
      param_map: Python `dict` mapping from the names given in `self.parameters`
        to the specified parameter values.
    """
    if hasattr(param_vals, 'keys'):
      param_map = param_vals
    else:
      param_map = {p.name: v for (p, v) in zip(self.parameters, param_vals)}

    return param_map

  def make_state_space_model(self,
                             num_timesteps,
                             param_vals,
                             initial_state_prior=None,
                             initial_step=0,
                             **linear_gaussian_ssm_kwargs):
    """Instantiate this model as a Distribution over specified `num_timesteps`.

    Args:
      num_timesteps: Python `int` number of timesteps to model.
      param_vals: a list of `Tensor` parameter values in order corresponding to
        `self.parameters`, or a dict mapping from parameter names to values.
      initial_state_prior: an optional `Distribution` instance overriding the
        default prior on the model's initial state. This is used in forecasting
        ("today's prior is yesterday's posterior").
      initial_step: optional `int` specifying the initial timestep to model.
        This is relevant when the model contains time-varying components,
        e.g., holidays or seasonality.
      **linear_gaussian_ssm_kwargs: Optional additional keyword arguments to
        to the base `tfd.LinearGaussianStateSpaceModel` constructor.

    Returns:
      dist: a `LinearGaussianStateSpaceModel` Distribution object.
    """
    return self._make_state_space_model(
        num_timesteps=num_timesteps,
        param_map=self._canonicalize_param_vals_as_map(param_vals),
        initial_state_prior=initial_state_prior,
        initial_step=initial_step,
        **linear_gaussian_ssm_kwargs)

  def prior_sample(self,
                   num_timesteps,
                   initial_step=0,
                   params_sample_shape=(),
                   trajectories_sample_shape=(),
                   seed=None):
    """Sample from the joint prior over model parameters and trajectories.

    Args:
      num_timesteps: Scalar `int` `Tensor` number of timesteps to model.
      initial_step: Optional scalar `int` `Tensor` specifying the starting
        timestep.
          Default value: 0.
      params_sample_shape: Number of possible worlds to sample iid from the
        parameter prior, or more generally, `Tensor` `int` shape to fill with
        iid samples.
          Default value: `[]` (i.e., draw a single sample and don't expand the
          shape).
      trajectories_sample_shape: For each sampled set of parameters, number
        of trajectories to sample, or more generally, `Tensor` `int` shape to
        fill with iid samples.
        Default value: `[]` (i.e., draw a single sample and don't expand the
          shape).
      seed: PRNG seed; see `tfp.random.sanitize_seed` for details.

    Returns:
      trajectories: `float` `Tensor` of shape
        `trajectories_sample_shape + params_sample_shape + [num_timesteps, 1]`
        containing all sampled trajectories.
      param_samples: list of sampled parameter value `Tensor`s, in order
        corresponding to `self.parameters`, each of shape
        `params_sample_shape + prior.batch_shape + prior.event_shape`.
    """

    seed = tfp_util.SeedStream(
        seed, salt='StructuralTimeSeries_prior_sample')

    with tf.name_scope('prior_sample'):
      param_samples = [
          p.prior.sample(params_sample_shape, seed=seed(), name=p.name)
          for p in self.parameters
      ]
      model = self.make_state_space_model(
          num_timesteps=num_timesteps,
          initial_step=initial_step,
          param_vals=param_samples)
      return model.sample(trajectories_sample_shape, seed=seed()), param_samples

  def joint_log_prob(self, observed_time_series):
    """Build the joint density `log p(params) + log p(y|params)` as a callable.

    Args:
      observed_time_series: Observed `Tensor` trajectories of shape
        `sample_shape + batch_shape + [num_timesteps, 1]` (the trailing
        `1` dimension is optional if `num_timesteps > 1`), where
        `batch_shape` should match `self.batch_shape` (the broadcast batch
        shape of all priors on parameters for this structural time series
        model). Any `NaN`s are interpreted as missing observations; missingness
        may be also be explicitly specified by passing a
        `tfp.sts.MaskedTimeSeries` instance.

    Returns:
     log_joint_fn: A function taking a `Tensor` argument for each model
       parameter, in canonical order, and returning a `Tensor` log probability
       of shape `batch_shape`. Note that, *unlike* `tfp.Distributions`
       `log_prob` methods, the `log_joint` sums over the `sample_shape` from y,
       so that `sample_shape` does not appear in the output log_prob. This
       corresponds to viewing multiple samples in `y` as iid observations from a
       single model, which is typically the desired behavior for parameter
       inference.
    """

    with tf.name_scope('joint_log_prob'):
      [
          observed_time_series,
          mask
      ] = sts_util.canonicalize_observed_time_series_with_mask(
          observed_time_series)

      num_timesteps = distribution_util.prefer_static_value(
          tf.shape(observed_time_series))[-2]

      def log_joint_fn(*param_vals, **param_kwargs):
        """Generated log-density function."""

        if param_kwargs:
          if param_vals: raise ValueError(
              'log_joint_fn saw both positional args ({}) and named args ({}). '
              'This is not supported: you have to choose!'.format(
                  param_vals, param_kwargs))
          param_vals = [param_kwargs[p.name] for p in self.parameters]

        # Sum the log_prob values from parameter priors.
        param_lp = sum([
            param.prior.log_prob(param_val)
            for (param, param_val) in zip(self.parameters, param_vals)
        ])

        # Build a linear Gaussian state space model and evaluate the marginal
        # log_prob on observations.
        lgssm = self.make_state_space_model(
            param_vals=param_vals, num_timesteps=num_timesteps)
        observation_lp = lgssm.log_prob(observed_time_series, mask=mask)

        # Sum over likelihoods from iid observations. Without this sum,
        # adding `param_lp + observation_lp` would broadcast the param priors
        # over the sample shape, which incorrectly multi-counts the param
        # priors.
        sample_ndims = tf.maximum(0,
                                  tf.rank(observation_lp) - tf.rank(param_lp))
        observation_lp = tf.reduce_sum(
            observation_lp, axis=tf.range(sample_ndims))

        return param_lp + observation_lp

    return log_joint_fn


def _strict_equals(a, b):
  try:
    return bool(a == b)
  except ValueError:
    # `a == b` is an array, Tensor, or similar.
    return id(a) == id(b)


def _assert_dict_contents_are_equal(
    a, b, message, ignore=(), equals_fn=_strict_equals):
  combined_keys = set(a.keys()) | set(b.keys())
  for k in combined_keys - set(ignore):
    a_val = a.get(k, None)
    b_val = b.get(k, None)
    if not _strict_equals(a_val, b_val):
      raise ValueError(message +
                       ' `{}`: {} vs {}.'.format(k, a_val, b_val))
