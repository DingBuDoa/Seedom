// SPDX-License-Identifier: BSD-3-Clause

#pragma once

#include "colmap/estimators/cost_functions/utils.h"

#include <ceres/ceres.h>

#include <cstddef>
#include <utility>
#include <vector>

namespace colmap {

// Smooths the normalized focal length between two camera parameter blocks.
// The parameter blocks are the complete Camera::params arrays. Keeping the
// complete blocks in the residual lets this work with every COLMAP camera
// model while only differentiating through the focal-length entries.
class DynamicFocalLengthDifferenceCostFunctor {
 public:
  DynamicFocalLengthDifferenceCostFunctor(std::vector<size_t> focal_indices,
                                          double weight)
      : focal_indices_(std::move(focal_indices)), weight_(weight) {}

  template <typename T>
  bool operator()(T const* const* parameters, T* residuals) const {
    residuals[0] = T(0);
    for (const size_t index : focal_indices_) {
      residuals[0] += parameters[0][index] - parameters[1][index];
    }
    residuals[0] *= T(weight_ / static_cast<double>(focal_indices_.size()));
    return true;
  }

  static ceres::CostFunction* Create(const std::vector<size_t>& focal_indices,
                                     size_t num_camera_params,
                                     double weight) {
    auto* cost = new ceres::DynamicAutoDiffCostFunction<
        DynamicFocalLengthDifferenceCostFunctor, 4>(
        new DynamicFocalLengthDifferenceCostFunctor(focal_indices, weight));
    cost->AddParameterBlock(static_cast<int>(num_camera_params));
    cost->AddParameterBlock(static_cast<int>(num_camera_params));
    cost->SetNumResiduals(1);
    return cost;
  }

 private:
  const std::vector<size_t> focal_indices_;
  const double weight_;
};

// Smooths the second temporal difference of normalized focal length. This
// penalizes acceleration of zoom and is intentionally separate from the
// first-order residual so callers can enable either constraint independently.
class DynamicFocalLengthSecondDifferenceCostFunctor {
 public:
  DynamicFocalLengthSecondDifferenceCostFunctor(
      std::vector<size_t> focal_indices, double weight)
      : focal_indices_(std::move(focal_indices)), weight_(weight) {}

  template <typename T>
  bool operator()(T const* const* parameters, T* residuals) const {
    residuals[0] = T(0);
    for (const size_t index : focal_indices_) {
      residuals[0] += parameters[0][index] - T(2) * parameters[1][index] +
                      parameters[2][index];
    }
    residuals[0] *= T(weight_ / static_cast<double>(focal_indices_.size()));
    return true;
  }

  static ceres::CostFunction* Create(const std::vector<size_t>& focal_indices,
                                     size_t num_camera_params,
                                     double weight) {
    auto* cost = new ceres::DynamicAutoDiffCostFunction<
        DynamicFocalLengthSecondDifferenceCostFunctor, 4>(
        new DynamicFocalLengthSecondDifferenceCostFunctor(focal_indices,
                                                           weight));
    cost->AddParameterBlock(static_cast<int>(num_camera_params));
    cost->AddParameterBlock(static_cast<int>(num_camera_params));
    cost->AddParameterBlock(static_cast<int>(num_camera_params));
    cost->SetNumResiduals(1);
    return cost;
  }

 private:
  const std::vector<size_t> focal_indices_;
  const double weight_;
};

}  // namespace colmap
