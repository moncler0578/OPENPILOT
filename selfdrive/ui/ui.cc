#include "selfdrive/ui/ui.h"

#include <string>
#include <cassert>
#include <cmath>

#include <QtConcurrent>
#include "common/transformations/orientation.hpp"
#include "selfdrive/common/params.h"
#include "selfdrive/common/swaglog.h"
#include "selfdrive/common/util.h"
#include "selfdrive/common/watchdog.h"
#include "selfdrive/hardware/hw.h"
#include "selfdrive/ui/qt/qt_window.h"

#define BACKLIGHT_DT 0.05
#define BACKLIGHT_TS 10.00
#define BACKLIGHT_OFFROAD 50

// Projects a point in car to space to the corresponding point in full frame
// image space.
static bool calib_frame_to_full_frame(const UIState *s, float in_x, float in_y, float in_z, QPointF *out) {
  const float margin = 500.0f;
  const QRectF clip_region{-margin, -margin, s->fb_w + 2 * margin, s->fb_h + 2 * margin};

  const vec3 pt = (vec3){{in_x, in_y, in_z}};
  const vec3 Ep = matvecmul3(s->scene.view_from_calib, pt);
  const vec3 KEp = matvecmul3(s->wide_camera ? ecam_intrinsic_matrix : fcam_intrinsic_matrix, Ep);

  // Project.
  QPointF point = s->car_space_transform.map(QPointF{KEp.v[0] / KEp.v[2], KEp.v[1] / KEp.v[2]});
  if (clip_region.contains(point)) {
    *out = point;
    return true;
  }
  return false;
}

static int get_path_length_idx(const cereal::ModelDataV2::XYZTData::Reader &line, const float path_height) {
  const auto line_x = line.getX();
  int max_idx = 0;
  for (int i = 1; i < TRAJECTORY_SIZE && line_x[i] <= path_height; ++i) {
    max_idx = i;
  }
  return max_idx;
}

static void update_leads(UIState *s, const cereal::RadarState::Reader &radar_state, const cereal::ModelDataV2::XYZTData::Reader &line) {
  for (int i = 0; i < 2; ++i) {
    auto lead_data = radar_state.getLeadOne();
    if (lead_data.getStatus()) {
      float z = line.getZ()[get_path_length_idx(line, lead_data.getDRel())];
      calib_frame_to_full_frame(s, lead_data.getDRel(), -lead_data.getYRel(), z + 1.22, &s->scene.lead_vertices[i]);
      s->scene.lead_radar[i] = lead_data.getRadar();
    }
    else
      s->scene.lead_radar[i] = false;
  }
}

static void update_line_data(const UIState *s, const cereal::ModelDataV2::XYZTData::Reader &line,
                             float y_off, float z_off, line_vertices_data *pvd, int max_idx, bool allow_invert=true) {
  const auto line_x = line.getX(), line_y = line.getY(), line_z = line.getZ();
  
  std::vector<QPointF> left_points, right_points;
  for (int i = 0; i <= max_idx; i++) {
    QPointF left, right;
    bool l = calib_frame_to_full_frame(s, line_x[i], line_y[i] - y_off, line_z[i] + z_off, &left);
    bool r = calib_frame_to_full_frame(s, line_x[i], line_y[i] + y_off, line_z[i] + z_off, &right);
    if (l && r) {
      // For wider lines the drawn polygon will "invert" when going over a hill and cause artifacts
      if (!allow_invert && left_points.size() && left.y() > left_points.back().y()) {
        continue;
      }
      left_points.push_back(left);
      right_points.push_back(right);
    }
  }

  pvd->cnt = 2 * left_points.size();
  assert(left_points.size() == right_points.size());
  assert(pvd->cnt <= std::size(pvd->v));

  for (int left_idx = 0; left_idx < left_points.size(); left_idx++){
    int right_idx = 2 * left_points.size() - left_idx - 1;
    pvd->v[left_idx] = left_points[left_idx];
    pvd->v[right_idx] = right_points[left_idx];
  }
}



static void update_blindspot_data(const UIState *s, int lr, const cereal::ModelDataV2::XYZTData::Reader &line,
                             float y_off,  line_vertices_data *pvd, int max_idx ) {
  float  y_off1, y_off2;

  float z_off_left = 0;  //def:0.0
  float z_off_right = 0;

  if( lr == 0 ) // left
  {
    y_off1 = y_off;
    y_off2 = 0;
  }
  else  // left
  {
      y_off1 = 0;
      y_off2 = y_off;  
  }


  const auto line_x = line.getX(), line_y = line.getY(), line_z = line.getZ();
  QPointF *v = &pvd->v[0]; // *v = &pvd->v[0];
  for (int i = 0; i <= max_idx; i++) {
    v += calib_frame_to_full_frame(s, line_x[i], line_y[i] - y_off1, line_z[i] + z_off_left, v);
  }
  for (int i = max_idx; i >= 0; i--) {
    v += calib_frame_to_full_frame(s, line_x[i], line_y[i] + y_off2, line_z[i] + z_off_right, v);
  }

  pvd->cnt = v - pvd->v;
  assert(pvd->cnt <= std::size(pvd->v));

}

static void update_model(UIState *s, const cereal::ModelDataV2::Reader &model) {
  UIScene &scene = s->scene;
  bool isCustomRoadUI = Params().getBool("CustomRoadUI");
  bool isUlimitedLength = isCustomRoadUI && Params().getBool("UnlimitedLength");
  auto model_position = model.getPosition();
  float max_distance = isUlimitedLength ? model_position.getX()[TRAJECTORY_SIZE - 1] : std::clamp(model_position.getX()[TRAJECTORY_SIZE - 1],
                                  MIN_DRAW_DISTANCE, MAX_DRAW_DISTANCE);
  const float pwidth = std::stof(Params().get("PathWidth")) / 10 * 0.1524;
  const float llwidth = std::stof(Params().get("LaneLinesWidth")) / 12 * 0.1524;
  const float rewidth = std::stof(Params().get("RoadEdgesWidth")) / 12 * 0.1524;
  const float blwidth = std::stof(Params().get("BlindspotLineWidth")) / 10 * 0.1524;

  // update lane lines
  const auto lane_lines = model.getLaneLines();
  const auto lane_line_probs = model.getLaneLineProbs();
  int max_idx = get_path_length_idx(lane_lines[0], max_distance);
  for (int i = 0; i < std::size(scene.lane_line_vertices); i++) {
    scene.lane_line_probs[i] = lane_line_probs[i];
    update_line_data(s, lane_lines[i], isCustomRoadUI ? llwidth * scene.lane_line_probs[i] : 0.025 * scene.lane_line_probs[i], 0, &scene.lane_line_vertices[i], max_idx);
  }

  // lane barriers for blind spot
  int max_distance_barrier =  100;
  int max_idx_barrier = std::min(max_idx, get_path_length_idx(lane_lines[0], max_distance_barrier));
  update_blindspot_data(s, 0, lane_lines[1], isCustomRoadUI ? blwidth : 0.5, &scene.lane_blindspot_vertices[0], max_idx_barrier);
  update_blindspot_data(s, 1, lane_lines[2], isCustomRoadUI ? blwidth : 0.5, &scene.lane_blindspot_vertices[1], max_idx_barrier);
  
  // update road edges
  const auto road_edges = model.getRoadEdges();
  const auto road_edge_stds = model.getRoadEdgeStds();
  for (int i = 0; i < std::size(scene.road_edge_vertices); i++) {
    scene.road_edge_stds[i] = road_edge_stds[i];
    update_line_data(s, road_edges[i], isCustomRoadUI ? rewidth : 0.025, 0, &scene.road_edge_vertices[i], max_idx);
  }

  // update path
  auto lead_one = (*s->sm)["radarState"].getRadarState().getLeadOne();
  if (lead_one.getStatus()) {
    const float lead_d = lead_one.getDRel() * 2.;
    max_distance = std::clamp((float)(lead_d - fmin(lead_d * 0.35, 10.)), 0.0f, max_distance);
  }
  max_idx = get_path_length_idx(model_position, max_distance);
  update_line_data(s, model_position, isCustomRoadUI ? pwidth : 0.9, 1.22, &scene.track_vertices, max_idx, false);
}

static void update_sockets(UIState *s) {
  s->sm->update(0);
}

static void update_state(UIState *s) {
  SubMaster &sm = *(s->sm);
  UIScene &scene = s->scene;
  
  if (sm.updated("carState")){
    scene.car_state = sm["carState"].getCarState();
    auto cs_data = sm["carState"].getCarState();
    scene.angleSteers = cs_data.getSteeringAngleDeg();
    scene.leftblindspot = scene.car_state.getLeftBlindspot();
    scene.rightblindspot = scene.car_state.getRightBlindspot();
  }
  
  if (scene.started && sm.updated("controlsState")) {
    scene.controls_state = sm["controlsState"].getControlsState();
    scene.lateralControlSelect = scene.controls_state.getLateralControlSelect();
    if (scene.lateralControlSelect == 0) {
      scene.output_scale = scene.controls_state.getLateralControlState().getPidState().getOutput();
    } else if (scene.lateralControlSelect == 1) {
      scene.output_scale = scene.controls_state.getLateralControlState().getIndiState().getOutput();
    } else if (scene.lateralControlSelect == 2) {
      scene.output_scale = scene.controls_state.getLateralControlState().getLqrState().getOutput();
    } else if (scene.lateralControlSelect == 3) {
      scene.output_scale = scene.controls_state.getLateralControlState().getTorqueState().getOutput();
    }  
  }
  if (sm.updated("liveCalibration")) {
    auto rpy_list = sm["liveCalibration"].getLiveCalibration().getRpyCalib();
    Eigen::Vector3d rpy;
    rpy << rpy_list[0], rpy_list[1], rpy_list[2];
    Eigen::Matrix3d device_from_calib = euler2rot(rpy);
    Eigen::Matrix3d view_from_device;
    view_from_device << 0,1,0,
                        0,0,1,
                        1,0,0;
    Eigen::Matrix3d view_from_calib = view_from_device * device_from_calib;
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        scene.view_from_calib.v[i*3 + j] = view_from_calib(i,j);
      }
    }
  }
  if (s->worldObjectsVisible()) {
    if (sm.updated("modelV2")) {
      update_model(s, sm["modelV2"].getModelV2());
    }
    if (sm.updated("radarState") && sm.rcv_frame("modelV2") > s->scene.started_frame) {
      update_leads(s, sm["radarState"].getRadarState(), sm["modelV2"].getModelV2().getPosition());
    }
  }
  if (sm.updated("pandaStates")) {
    auto pandaStates = sm["pandaStates"].getPandaStates();
    if (pandaStates.size() > 0) {
      scene.pandaType = pandaStates[0].getPandaType();

      if (scene.pandaType != cereal::PandaState::PandaType::UNKNOWN) {
        scene.ignition = false;
        for (const auto& pandaState : pandaStates) {
          scene.ignition |= pandaState.getIgnitionLine() || pandaState.getIgnitionCan();
        }
      }
    }
  } else if ((s->sm->frame - s->sm->rcv_frame("pandaStates")) > 5*UI_FREQ) {
    scene.pandaType = cereal::PandaState::PandaType::UNKNOWN;
  }
  if (sm.updated("carParams")) {
    scene.longitudinal_control = sm["carParams"].getCarParams().getOpenpilotLongitudinalControl();
  }
  if (!scene.started && sm.updated("sensorEvents")) {
    for (auto sensor : sm["sensorEvents"].getSensorEvents()) {
      if (sensor.which() == cereal::SensorEventData::ACCELERATION) {
        auto accel = sensor.getAcceleration().getV();
        if (accel.totalSize().wordCount) { // TODO: sometimes empty lists are received. Figure out why
          scene.accel_sensor = accel[2];
        }
      } else if (sensor.which() == cereal::SensorEventData::GYRO_UNCALIBRATED) {
        auto gyro = sensor.getGyroUncalibrated().getV();
        if (gyro.totalSize().wordCount) {
          scene.gyro_sensor = gyro[1];
        }
      }
    }
  }
  if (!Hardware::TICI() && sm.updated("roadCameraState")) {
    auto camera_state = sm["roadCameraState"].getRoadCameraState();

    float max_lines = Hardware::EON() ? 5408 : 1904;
    float max_gain = Hardware::EON() ? 1.0: 10.0;
    float max_ev = max_lines * max_gain;

    float ev = camera_state.getGain() * float(camera_state.getIntegLines());

    scene.light_sensor = std::clamp<float>(1.0 - (ev / max_ev), 0.0, 1.0);
  } else if (Hardware::TICI() && sm.updated("wideRoadCameraState")) {
    auto camera_state = sm["wideRoadCameraState"].getWideRoadCameraState();

    float max_lines = 1618;
    float max_gain = 10.0;
    float max_ev = max_lines * max_gain / 6;

    float ev = camera_state.getGain() * float(camera_state.getIntegLines());

    scene.light_sensor = std::clamp<float>(1.0 - (ev / max_ev), 0.0, 1.0);
  }
  scene.started = sm["deviceState"].getDeviceState().getStarted() && scene.ignition;
  if (sm.updated("lateralPlan")) {
    auto data = sm["lateralPlan"].getLateralPlan();

    scene.lateralPlan.dynamicLaneProfileStatus = data.getDynamicLaneProfile();
  }
}

void ui_update_params(UIState *s) {
  Params params;
  s->scene.is_metric = params.getBool("IsMetric");
  s->scene.compass = params.getBool("Compass");
  s->show_debug = params.getBool("ShowDebugUI");
  s->show_gear = params.getBool("ShowCgearUI");//기어
  s->show_tpms = params.getBool("ShowTpmsUI");
  s->show_brake = params.getBool("ShowBrakeUI");
  s->show_engrpm = params.getBool("ShowEngRPMUI");
  s->show_datetime = params.getBool("ShowDateTime");
  s->show_steer = params.getBool("ShowSteerUI");
}

void UIState::updateStatus() {
  if (scene.started && sm->updated("controlsState")) {
    auto controls_state = (*sm)["controlsState"].getControlsState();
    auto alert_status = controls_state.getAlertStatus();
    auto state = controls_state.getState();
    if (alert_status == cereal::ControlsState::AlertStatus::USER_PROMPT) {
      status = STATUS_WARNING;
    } else if (alert_status == cereal::ControlsState::AlertStatus::CRITICAL) {
      status = STATUS_ALERT;
    } else if (state == cereal::ControlsState::OpenpilotState::PRE_ENABLED || state == cereal::ControlsState::OpenpilotState::OVERRIDING) {
      status = STATUS_OVERRIDE;
    } else {
      status = controls_state.getEnabled() ? STATUS_ENGAGED : STATUS_DISENGAGED;
    }
  }

  // Handle onroad/offroad transition
  if (scene.started != started_prev || sm->frame == 1) {
    if (scene.started) {
      status = STATUS_DISENGAGED;
      scene.started_frame = sm->frame;
      scene.end_to_end = Params().getBool("EndToEndToggle");
      wide_camera = Hardware::TICI() ? Params().getBool("EnableWideCamera") : false;
      scene.dynamic_lane_profile = std::stoi(Params().get("DynamicLaneProfile"));
    }
    started_prev = scene.started;
    emit offroadTransition(!scene.started);
  }
}

UIState::UIState(QObject *parent) : QObject(parent) {
  sm = std::make_unique<SubMaster, const std::initializer_list<const char *>>({
    "modelV2", "controlsState", "liveCalibration", "radarState", "deviceState", "roadCameraState",
    "pandaStates", "carParams", "driverMonitoringState", "sensorEvents", "carState", "liveLocationKalman",
    "wideRoadCameraState",
    "gpsLocationExternal", "carControl", "liveParameters", "lateralPlan", "roadLimitSpeed",
  });

  Params params;
  wide_camera = Hardware::TICI() ? params.getBool("EnableWideCamera") : false;
  prime_type = std::atoi(params.get("PrimeType").c_str());
  language = QString::fromStdString(params.get("LanguageSetting"));

  // update timer
  timer = new QTimer(this);
  QObject::connect(timer, &QTimer::timeout, this, &UIState::update);
  timer->start(1000 / UI_FREQ);
}

void UIState::update() {
  update_sockets(this);
  update_state(this);
  updateStatus();

  if (sm->frame % UI_FREQ == 0) {
    watchdog_kick();
  }
  emit uiUpdate(*this);
}

Device::Device(QObject *parent) : brightness_filter(BACKLIGHT_OFFROAD, BACKLIGHT_TS, BACKLIGHT_DT), QObject(parent) {
  setAwake(true);
  resetInteractiveTimout();

  QObject::connect(uiState(), &UIState::uiUpdate, this, &Device::update);
}

void Device::update(const UIState &s) {
  updateBrightness(s);
  updateWakefulness(s);

  // TODO: remove from UIState and use signals
  uiState()->awake = awake;
}

void Device::setAwake(bool on) {
  if (on != awake) {
    awake = on;
    Hardware::set_display_power(awake);
    LOGD("setting display power %d", awake);
    emit displayPowerChanged(awake);
  }
}

void Device::resetInteractiveTimout() {
  interactive_timeout = (ignition_on ? 10 : 30) * UI_FREQ;
}

void Device::updateBrightness(const UIState &s) {
  float clipped_brightness = BACKLIGHT_OFFROAD;
  if (s.scene.started) {
    // Scale to 0% to 100%
    clipped_brightness = 100.0 * s.scene.light_sensor;

    // CIE 1931 - https://www.photonstophotos.net/GeneralTopics/Exposure/Psychometric_Lightness_and_Gamma.htm
    if (clipped_brightness <= 8) {
      clipped_brightness = (clipped_brightness / 903.3);
    } else {
      clipped_brightness = std::pow((clipped_brightness + 16.0) / 116.0, 3.0);
    }

    // Scale back to 10% to 100%
    clipped_brightness = std::clamp(100.0f * clipped_brightness, 10.0f, 100.0f);
  }

  int brightness = brightness_filter.update(clipped_brightness);
  if (!awake) {
    brightness = 0;
  }

  if (brightness != last_brightness) {
    if (!brightness_future.isRunning()) {
      brightness_future = QtConcurrent::run(Hardware::set_brightness, brightness);
      last_brightness = brightness;
    }
  }
}

bool Device::motionTriggered(const UIState &s) {
  static float accel_prev = 0;
  static float gyro_prev = 0;

  bool accel_trigger = abs(s.scene.accel_sensor - accel_prev) > 0.2;
  bool gyro_trigger = abs(s.scene.gyro_sensor - gyro_prev) > 0.15;

  gyro_prev = s.scene.gyro_sensor;
  accel_prev = (accel_prev * (accel_samples - 1) + s.scene.accel_sensor) / accel_samples;

  return (!awake && accel_trigger && gyro_trigger);
}

void Device::updateWakefulness(const UIState &s) {
  bool ignition_just_turned_off = !s.scene.ignition && ignition_on;
  ignition_on = s.scene.ignition;

  if (ignition_just_turned_off || motionTriggered(s)) {
    resetInteractiveTimout();
  } else if (interactive_timeout > 0 && --interactive_timeout == 0) {
    emit interactiveTimout();
  }

  setAwake(s.scene.ignition || interactive_timeout > 0);
}

UIState *uiState() {
  static UIState ui_state;
  return &ui_state;
}
