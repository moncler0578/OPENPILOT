#!/usr/bin/env python3
import os
import numpy as np

from common.realtime import sec_since_boot
from common.numpy_fast import clip, interp
from selfdrive.swaglog import cloudlog
from selfdrive.modeld.constants import index_function
from selfdrive.controls.radard import _LEAD_ACCEL_TAU
from selfdrive.controls.ntune import ntune_scc_get
from common.conversions import Conversions as CV
from common.params import Params
from common.realtime import DT_MDL
from common.filter_simple import FirstOrderFilter

if __name__ == '__main__':  # generating code
  from pyextra.acados_template import AcadosModel, AcadosOcp, AcadosOcpSolver
else:
  from selfdrive.controls.lib.longitudinal_mpc_lib.c_generated_code.acados_ocp_solver_pyx import AcadosOcpSolverCython  # pylint: disable=no-name-in-module, import-error

from casadi import SX, vertcat

MODEL_NAME = 'long'
LONG_MPC_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(LONG_MPC_DIR, "c_generated_code")
JSON_FILE = os.path.join(LONG_MPC_DIR, "acados_ocp_long.json")

SOURCES = ['lead0', 'lead1', 'cruise']

X_DIM = 3
U_DIM = 1
PARAM_DIM = 8
COST_E_DIM = 5
COST_DIM = COST_E_DIM + 1
CONSTR_DIM = 4

X_EGO_OBSTACLE_COST = 4.
X_EGO_COST = 0.
V_EGO_COST = 0.
A_EGO_COST = 0.
J_EGO_COST = 5.0
A_CHANGE_COST = 150.
DANGER_ZONE_COST = 100.
CRASH_DISTANCE = .5
LEAD_DANGER_FACTOR = 0.8
LIMIT_COST = 1e6
ACADOS_SOLVER_TYPE = 'SQP_RTI'

DIFF_RADAR_VISION = 2.0

# Fewer timestamps don't hurt performance and lead to
# much better convergence of the MPC with low iterations
N = 12
MAX_T = 10.0
T_IDXS_LST = [index_function(idx, max_val=MAX_T, max_idx=N) for idx in range(N+1)]

T_IDXS = np.array(T_IDXS_LST)
T_DIFFS = np.diff(T_IDXS, prepend=[0.])
MIN_ACCEL = -3.5
T_FOLLOW = 1.45
COMFORT_BRAKE = 2.5
STOP_DISTANCE = 6.0

def get_stopped_equivalence_factor(v_lead, v_ego, t_follow=T_FOLLOW, stop_distance=STOP_DISTANCE):
  # KRKeegan this offset rapidly decreases the following distance when the lead pulls
  # away, resulting in an early demand for acceleration.
  v_diff_offset = 0
  if np.all(v_lead - v_ego > 0):
    v_diff_offset = ((v_lead - v_ego) * 1.)
    v_diff_offset = np.clip(v_diff_offset, 0, stop_distance / 2)
    v_diff_offset = np.maximum(v_diff_offset * ((10 - v_ego)/10), 0)
  distance = (v_lead**2) / (2 * COMFORT_BRAKE) + v_diff_offset
  return distance
  
def get_safe_obstacle_distance(v_ego, t_follow=T_FOLLOW, comfort_brake=COMFORT_BRAKE, stop_distance=STOP_DISTANCE):
  return (v_ego**2) / (2 * comfort_brake) +  t_follow * v_ego + stop_distance

def desired_follow_distance(v_ego, v_lead, t_follow=T_FOLLOW):
  return get_safe_obstacle_distance(v_ego, t_follow) - get_stopped_equivalence_factor(v_lead, v_ego, t_follow)


def gen_long_model():
  model = AcadosModel()
  model.name = MODEL_NAME

  # set up states & controls
  x_ego = SX.sym('x_ego')
  v_ego = SX.sym('v_ego')
  a_ego = SX.sym('a_ego')
  model.x = vertcat(x_ego, v_ego, a_ego)

  # controls
  j_ego = SX.sym('j_ego')
  model.u = vertcat(j_ego)

  # xdot
  x_ego_dot = SX.sym('x_ego_dot')
  v_ego_dot = SX.sym('v_ego_dot')
  a_ego_dot = SX.sym('a_ego_dot')
  model.xdot = vertcat(x_ego_dot, v_ego_dot, a_ego_dot)

  # live parameters
  a_min = SX.sym('a_min')
  a_max = SX.sym('a_max')
  x_obstacle = SX.sym('x_obstacle')
  prev_a = SX.sym('prev_a')
  lead_t_follow = SX.sym('lead_t_follow')
  comfort_brake = SX.sym('comfort_brake')
  stop_distance = SX.sym('stop_distance')
  lead_danger_factor = SX.sym('lead_danger_factor')
  model.p = vertcat(a_min, a_max, x_obstacle, prev_a, lead_t_follow, comfort_brake, stop_distance, lead_danger_factor)

  # dynamics model
  f_expl = vertcat(v_ego, a_ego, j_ego)
  model.f_impl_expr = model.xdot - f_expl
  model.f_expl_expr = f_expl
  return model


def gen_long_ocp():
  ocp = AcadosOcp()
  ocp.model = gen_long_model()

  Tf = T_IDXS[-1]

  # set dimensions
  ocp.dims.N = N

  # set cost module
  ocp.cost.cost_type = 'NONLINEAR_LS'
  ocp.cost.cost_type_e = 'NONLINEAR_LS'

  QR = np.zeros((COST_DIM, COST_DIM))
  Q = np.zeros((COST_E_DIM, COST_E_DIM))

  ocp.cost.W = QR
  ocp.cost.W_e = Q

  x_ego, v_ego, a_ego = ocp.model.x[0], ocp.model.x[1], ocp.model.x[2]
  j_ego = ocp.model.u[0]

  a_min, a_max = ocp.model.p[0], ocp.model.p[1]
  x_obstacle = ocp.model.p[2]
  prev_a = ocp.model.p[3]
  lead_t_follow = ocp.model.p[4]
  comfort_brake = ocp.model.p[5]
  stop_distance = ocp.model.p[6]
  lead_danger_factor = ocp.model.p[7]

  ocp.cost.yref = np.zeros((COST_DIM, ))
  ocp.cost.yref_e = np.zeros((COST_E_DIM, ))

  desired_dist_comfort = get_safe_obstacle_distance(v_ego, lead_t_follow, comfort_brake, stop_distance)

  # The main cost in normal operation is how close you are to the "desired" distance
  # from an obstacle at every timestep. This obstacle can be a lead car
  # or other object. In e2e mode we can use x_position targets as a cost
  # instead.
  costs = [((x_obstacle - x_ego) - (desired_dist_comfort)) / (v_ego + 10.),
           x_ego,
           v_ego,
           a_ego,
           a_ego - prev_a,
           j_ego]
  ocp.model.cost_y_expr = vertcat(*costs)
  ocp.model.cost_y_expr_e = vertcat(*costs[:-1])

  # Constraints on speed, acceleration and desired distance to
  # the obstacle, which is treated as a slack constraint so it
  # behaves like an asymmetrical cost.
  constraints = vertcat(v_ego,
                        (a_ego - a_min),
                        (a_max - a_ego),
                        ((x_obstacle - x_ego) - lead_danger_factor * (desired_dist_comfort)) / (v_ego + 10.))
  ocp.model.con_h_expr = constraints

  x0 = np.zeros(X_DIM)
  ocp.constraints.x0 = x0
  ocp.parameter_values = np.array([-1.2, 1.2, 0.0, 0.0, T_FOLLOW, COMFORT_BRAKE, STOP_DISTANCE, LEAD_DANGER_FACTOR])

  # We put all constraint cost weights to 0 and only set them at runtime
  cost_weights = np.zeros(CONSTR_DIM)
  ocp.cost.zl = cost_weights
  ocp.cost.Zl = cost_weights
  ocp.cost.Zu = cost_weights
  ocp.cost.zu = cost_weights

  ocp.constraints.lh = np.zeros(CONSTR_DIM)
  ocp.constraints.uh = 1e4*np.ones(CONSTR_DIM)
  ocp.constraints.idxsh = np.arange(CONSTR_DIM)

  # The HPIPM solver can give decent solutions even when it is stopped early
  # Which is critical for our purpose where compute time is strictly bounded
  # We use HPIPM in the SPEED_ABS mode, which ensures fastest runtime. This
  # does not cause issues since the problem is well bounded.
  ocp.solver_options.qp_solver = 'PARTIAL_CONDENSING_HPIPM'
  ocp.solver_options.hessian_approx = 'GAUSS_NEWTON'
  ocp.solver_options.integrator_type = 'ERK'
  ocp.solver_options.nlp_solver_type = ACADOS_SOLVER_TYPE
  ocp.solver_options.qp_solver_cond_N = 1

  # More iterations take too much time and less lead to inaccurate convergence in
  # some situations. Ideally we would run just 1 iteration to ensure fixed runtime.
  ocp.solver_options.qp_solver_iter_max = 10
  ocp.solver_options.qp_tol = 1e-3

  # set prediction horizon
  ocp.solver_options.tf = Tf
  ocp.solver_options.shooting_nodes = T_IDXS

  ocp.code_export_directory = EXPORT_DIR
  return ocp


class LongitudinalMpc:
  def __init__(self, e2e=False, dt=DT_MDL):
    self.dt = dt
    self.e2e = e2e
    self.JEgoCost = 5.
    self.DangerZoneCost = 100.
    self.leadDangerFactor = LEAD_DANGER_FACTOR
    self.XEgoObstacleCost = 3.
    self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    self.reset()
    self.lo_timer = 0
    self.v_cruise = 0.
    self.t_follow = T_FOLLOW
    
    self.source = SOURCES[2]

  def reset(self):
    # self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    self.solver.reset()
    # self.solver.options_set('print_level', 2)
    self.x_solution = np.zeros(N+1)
    self.v_solution = np.zeros(N+1)
    self.a_solution = np.zeros(N+1)
    self.prev_a = np.array(self.a_solution)
    self.j_solution = np.zeros(N)
    self.yref = np.zeros((N+1, COST_DIM))
    for i in range(N):
      self.solver.cost_set(i, "yref", self.yref[i])
    self.solver.cost_set(N, "yref", self.yref[N][:COST_E_DIM])
    self.x_sol = np.zeros((N+1, X_DIM))
    self.u_sol = np.zeros((N,1))
    self.params = np.zeros((N+1, PARAM_DIM))
    self.t_follow = T_FOLLOW
    
    for i in range(N+1):
      self.solver.set(i, 'x', np.zeros(X_DIM))
    self.last_cloudlog_t = 0
    self.status = False
    self.crash_cnt = 0.0
    self.solution_status = 0
    # timers
    self.solve_time = 0.0
    self.time_qp_solution = 0.0
    self.time_linearization = 0.0
    self.time_integrator = 0.0
    self.x0 = np.zeros(X_DIM)
    self.set_weights()

  def set_weights(self, prev_accel_constraint=True, v_lead0=0, v_lead1=0):
    if self.e2e:
      self.set_weights_for_xva_policy()
      self.params[:,0] = -10.
      self.params[:,1] = 10.
      self.params[:,2] = 1e5
    else:
      self.set_weights_for_lead_policy(prev_accel_constraint, v_lead0, v_lead1)

  def get_cost_multipliers(self, v_lead0, v_lead1):
    v_ego = self.x0[1]
    v_ego_bps = [0, 10]
    TFs = [1.0, 1.25, T_FOLLOW]
    # KRKeegan adjustments to costs for different TFs
    # these were calculated using the test_longitudial.py deceleration tests
    a_change_tf = interp(self.t_follow, TFs, [.1, .8, 1.])
    j_ego_tf = interp(self.t_follow, TFs, [.6, .8, 1.])
    d_zone_tf = interp(self.t_follow, TFs, [1.6, 1.3, 1.])
    # KRKeegan adjustments to improve sluggish acceleration
    # do not apply to deceleration
    j_ego_v_ego = 1
    a_change_v_ego = 1
    if (v_lead0 - v_ego >= 0) and (v_lead1 - v_ego >= 0):
      j_ego_v_ego = interp(v_ego, v_ego_bps, [.05, 1.])
      a_change_v_ego = interp(v_ego, v_ego_bps, [.05, 1.])
    # Select the appropriate min/max of the options
    j_ego = min(j_ego_tf, j_ego_v_ego)
    a_change = min(a_change_tf, a_change_v_ego)
    return (a_change, j_ego, d_zone_tf)

  def set_weights_for_lead_policy(self, prev_accel_constraint=True, v_lead0=0, v_lead1=0):
    a_change_cost = A_CHANGE_COST if prev_accel_constraint else 0
    cost_mulitpliers = self.get_cost_multipliers(v_lead0, v_lead1)
    W = np.asfortranarray(np.diag([self.XEgoObstacleCost, X_EGO_COST, V_EGO_COST,
                                   A_EGO_COST, a_change_cost * cost_mulitpliers[0],
                                   J_EGO_COST * cost_mulitpliers[1]]))
    for i in range(N):
      # reduce the cost on (a-a_prev) later in the horizon.
      W[4,4] = a_change_cost * cost_mulitpliers[0] * np.interp(T_IDXS[i], [0.0, 1.0, 2.0], [1.0, 1.0, 0.0])
      self.solver.cost_set(i, 'W', W)
    # Setting the slice without the copy make the array not contiguous,
    # causing issues with the C interface.
    self.solver.cost_set(N, 'W', np.copy(W[:COST_E_DIM, :COST_E_DIM]))

    # Set L2 slack cost on lower bound constraints
    Zl = np.array([LIMIT_COST, LIMIT_COST, LIMIT_COST, self.DangerZoneCost * cost_mulitpliers[2]])
    for i in range(N):
      self.solver.cost_set(i, 'Zl', Zl)

  def set_weights_for_xva_policy(self):
    W = np.asfortranarray(np.diag([0., 0.2, 0.25, 1., 0.0, .1]))
    for i in range(N):
      self.solver.cost_set(i, 'W', W)
    # Setting the slice without the copy make the array not contiguous,
    # causing issues with the C interface.
    self.solver.cost_set(N, 'W', np.copy(W[:COST_E_DIM, :COST_E_DIM]))

    # Set L2 slack cost on lower bound constraints
    Zl = np.array([LIMIT_COST, LIMIT_COST, LIMIT_COST, 0.0])
    for i in range(N):
      self.solver.cost_set(i, 'Zl', Zl)

  def set_cur_state(self, v, a):
    v_prev = self.x0[1]
    self.x0[1] = v
    self.x0[2] = a
    if abs(v_prev - v) > 2.: # probably only helps if v < v_prev
      for i in range(0, N+1):
        self.solver.set(i, 'x', self.x0)

  @staticmethod
  def extrapolate_lead(x_lead, v_lead, a_lead, a_lead_tau):
    a_lead_traj = a_lead * np.exp(-a_lead_tau * (T_IDXS**2)/2.)
    v_lead_traj = np.clip(v_lead + np.cumsum(T_DIFFS * a_lead_traj), 0.0, 1e8)
    x_lead_traj = x_lead + np.cumsum(T_DIFFS * v_lead_traj)
    lead_xv = np.column_stack((x_lead_traj, v_lead_traj))
    return lead_xv

  def process_lead(self, lead):
    v_ego = self.x0[1]
    if lead is not None and lead.status:
      x_lead = lead.dRel
      v_lead = lead.vLead
      a_lead = lead.aLeadK
      a_lead_tau = lead.aLeadTau
    else:
      # Fake a fast lead car, so mpc can keep running in the same mode
      x_lead = 50.0
      v_lead = v_ego + 10.0
      a_lead = 0.0
      a_lead_tau = _LEAD_ACCEL_TAU

    # MPC will not converge if immediate crash is expected
    # Clip lead distance to what is still possible to brake for
    min_x_lead = ((v_ego + v_lead)/2) * (v_ego - v_lead) / (-MIN_ACCEL * 2)
    x_lead = clip(x_lead, min_x_lead, 1e8)
    v_lead = clip(v_lead, 0.0, 1e8)
    a_lead = clip(a_lead, -10., 5.)
    lead_xv = self.extrapolate_lead(x_lead, v_lead, a_lead, a_lead_tau)
    return lead_xv

  def set_accel_limits(self, min_a, max_a):
    self.cruise_min_a = min_a
    self.cruise_max_a = max_a

  def update(self, carstate, radarstate, v_cruise, prev_accel_constraint):
    
    self.update_params()
    v_ego = self.x0[1]
    a_ego = self.x0[2]
    a_ego = carstate.aEgo
      
    self.status = radarstate.leadOne.status or radarstate.leadTwo.status

    lead_xv_0 = self.process_lead(radarstate.leadOne)
    lead_xv_1 = self.process_lead(radarstate.leadTwo)

    self.update_TF(carstate, radarstate, v_ego, a_ego)
    comfort_brake = ntune_scc_get('comfortBrake')
    stop_distance = ntune_scc_get('stopDistance')
    
    self.set_weights(prev_accel_constraint=prev_accel_constraint, v_lead0=lead_xv_0[0,1], v_lead1=lead_xv_1[0,1])

    # set accel limits in params
    self.params[:,0] = interp(float(self.status), [0.0, 1.0], [self.cruise_min_a, MIN_ACCEL])
    self.params[:,1] = self.cruise_max_a
      
    # To estimate a safe distance from a moving lead, we calculate how much stopping
    # distance that lead needs as a minimum. We can add that to the current distance
    # and then treat that as a stopped car/obstacle at this new distance.
    lead_0_obstacle = lead_xv_0[:,0] + get_stopped_equivalence_factor(lead_xv_0[:,1], self.x_sol[:,1], self.t_follow, stop_distance)
    lead_1_obstacle = lead_xv_1[:,0] + get_stopped_equivalence_factor(lead_xv_1[:,1], self.x_sol[:,1], self.t_follow, stop_distance)


    # Fake an obstacle for cruise, this ensures smooth acceleration to set speed
    # when the leads are no factor.
    v_lower = v_ego + (T_IDXS * self.cruise_min_a * 1.05)
    v_upper = v_ego + (T_IDXS * self.cruise_max_a * 1.05)
    v_cruise_clipped = np.clip(v_cruise * np.ones(N+1),
                               v_lower,
                               v_upper)
    cruise_obstacle = np.cumsum(T_DIFFS * v_cruise_clipped) + get_safe_obstacle_distance(v_cruise_clipped, self.t_follow, comfort_brake, stop_distance)

    x_obstacles = np.column_stack([lead_0_obstacle, lead_1_obstacle, cruise_obstacle])
    self.source = SOURCES[np.argmin(x_obstacles[0])]
    self.params[:,2] = np.min(x_obstacles, axis=1)
    self.params[:,3] = np.copy(self.prev_a)
    self.params[:,4] = self.t_follow
    self.params[:,5] = comfort_brake
    self.params[:,6] = stop_distance
    self.params[:,7] = self.leadDangerFactor
    
    self.run()
    if (np.any(lead_xv_0[:,0] - self.x_sol[:,0] < CRASH_DISTANCE) and
            radarstate.leadOne.modelProb > 0.9):
      self.crash_cnt += 1
    else:
      self.crash_cnt = 0

  def update_with_xva(self, x, v, a):
    # v, and a are in local frame, but x is wrt the x[0] position
    # In >90degree turns, x goes to 0 (and may even be -ve)
    # So, we use integral(v) + x[0] to obtain the forward-distance
    xforward = ((v[1:] + v[:-1]) / 2) * (T_IDXS[1:] - T_IDXS[:-1])
    x = np.cumsum(np.insert(xforward, 0, x[0]))
    self.yref[:,1] = x
    self.yref[:,2] = v
    self.yref[:,3] = a
    for i in range(N):
      self.solver.cost_set(i, "yref", self.yref[i])
    self.solver.cost_set(N, "yref", self.yref[N][:COST_E_DIM])
    self.params[:,3] = np.copy(self.prev_a)
    self.run()

  def run(self):
    # t0 = sec_since_boot()
    # reset = 0
    for i in range(N+1):
      self.solver.set(i, 'p', self.params[i])
    self.solver.constraints_set(0, "lbx", self.x0)
    self.solver.constraints_set(0, "ubx", self.x0)

    self.solution_status = self.solver.solve()
    self.solve_time = float(self.solver.get_stats('time_tot')[0])
    self.time_qp_solution = float(self.solver.get_stats('time_qp')[0])
    self.time_linearization = float(self.solver.get_stats('time_lin')[0])
    self.time_integrator = float(self.solver.get_stats('time_sim')[0])

    # qp_iter = self.solver.get_stats('statistics')[-1][-1] # SQP_RTI specific
    # print(f"long_mpc timings: tot {self.solve_time:.2e}, qp {self.time_qp_solution:.2e}, lin {self.time_linearization:.2e}, integrator {self.time_integrator:.2e}, qp_iter {qp_iter}")
    # res = self.solver.get_residuals()
    # print(f"long_mpc residuals: {res[0]:.2e}, {res[1]:.2e}, {res[2]:.2e}, {res[3]:.2e}")
    # self.solver.print_statistics()

    for i in range(N+1):
      self.x_sol[i] = self.solver.get(i, 'x')
    for i in range(N):
      self.u_sol[i] = self.solver.get(i, 'u')

    self.x_solution = self.x_sol[:,0]
    self.v_solution = self.x_sol[:,1]
    self.a_solution = self.x_sol[:,2]
    self.j_solution = self.u_sol[:,0]

    self.prev_a = np.interp(T_IDXS + self.dt, T_IDXS, self.a_solution)

    t = sec_since_boot()
    if self.solution_status != 0:
      if t > self.last_cloudlog_t + 5.0:
        self.last_cloudlog_t = t
        cloudlog.warning(f"Long mpc reset, solution_status: {self.solution_status}")
      self.reset()
      # reset = 1
    # print(f"long_mpc timings: total internal {self.solve_time:.2e}, external: {(sec_since_boot() - t0):.2e} qp {self.time_qp_solution:.2e}, lin {self.time_linearization:.2e} qp_iter {qp_iter}, reset {reset}")

  def update_params(self):
    self.lo_timer += 1
    if self.lo_timer > 200:
      self.lo_timer = 0
    elif self.lo_timer == 20:
      pass
    elif self.lo_timer == 40:
      self.XEgoObstacleCost = float(int(Params().get("XEgoObstacleCost", encoding="utf8")))
      self.JEgoCost = float(int(Params().get("JEgoCost", encoding="utf8")))
    elif self.lo_timer == 60:
      self.DangerZoneCost = float(int(Params().get("DangerZoneCost", encoding="utf8")))
    elif self.lo_timer == 80:
      self.leadDangerFactor = float(int(Params().get("LeadDangerFactor", encoding="utf8"))) * 0.01

  def update_TF(self, carstate, radarstate, v_ego, a_ego):
    cruise_gap = int(clip(carstate.cruiseGap, 1., 4.))
    if cruise_gap == 1:
      self.t_follow = 1.0
    elif cruise_gap == 2:
      self.t_follow = 1.25
    elif cruise_gap == 3:
      x_vel = [0,    11,   13,   15,   25,   40]
      y_dist = [1.75, 1.8, 1.9, 2.0, 2.2,  2.3]
      self.t_follow = np.interp(carstate.vEgo, x_vel, y_dist)
    elif cruise_gap == 4:
      x_vel = [0,    11,   13,   15,   25,   40]
      y_dist = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
      self.t_follow = np.interp(carstate.vEgo, x_vel, y_dist)

if __name__ == "__main__":
  ocp = gen_long_ocp()
  AcadosOcpSolver.generate(ocp, json_file=JSON_FILE)
  # AcadosOcpSolver.build(ocp.code_export_directory, with_cython=True)
