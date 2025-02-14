#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import torch
import math
from diff_gaussian_rasterization import GaussianRasterizationSettings, GaussianRasterizer
from scene.gaussian_model import GaussianModel
from utils.sh_utils import eval_sh
from time import time as get_time


def render(viewpoint_camera, pc: GaussianModel, pipe, bg_color: torch.Tensor, scaling_modifier=1.0, override_color=None,
           stage="fine", cam_type=None):
    """
    Render the scene.

    Background tensor (bg_color) must be on GPU!
    """

    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        screenspace_points.retain_grad()
    except:
        pass

    # Set up rasterization configuration
    means3D = pc.get_xyz
    if cam_type != "PanopticSports":
        tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
        tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)
        raster_settings = GaussianRasterizationSettings(
            image_height=int(viewpoint_camera.image_height),
            image_width=int(viewpoint_camera.image_width),
            tanfovx=tanfovx,
            tanfovy=tanfovy,
            bg=bg_color,
            scale_modifier=scaling_modifier,
            viewmatrix=viewpoint_camera.world_view_transform.cuda(),
            projmatrix=viewpoint_camera.full_proj_transform.cuda(),
            sh_degree=pc.active_sh_degree,
            campos=viewpoint_camera.camera_center.cuda(),
            prefiltered=False,
            debug=pipe.debug
        )
        time = torch.tensor(viewpoint_camera.time).to(means3D.device).repeat(means3D.shape[0], 1)
    else:
        raster_settings = viewpoint_camera['camera']
        time = torch.tensor(viewpoint_camera['time']).to(means3D.device).repeat(means3D.shape[0], 1)

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)


    means2D = screenspace_points
    opacity = pc._opacity
    shs = pc.get_features

    # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
    # scaling / rotation by the rasterizer.
    scales = None
    rotations = None
    cov3D_precomp = None
    if pipe.compute_cov3D_python:
        cov3D_precomp = pc.get_covariance(scaling_modifier)
    else:
        scales = pc._scaling
        rotations = pc._rotation
    deformation_point = pc._deformation_table
    if "coarse" in stage:
        means3D_final, scales_final, rotations_final, opacity_final, shs_final = means3D, scales, rotations, opacity, shs
    elif "fine" in stage:
        # time0 = get_time()
        # means3D_deform, scales_deform, rotations_deform, opacity_deform = pc._deformation(means3D[deformation_point], scales[deformation_point],
        #                                                                  rotations[deformation_point], opacity[deformation_point],
        #                                                                  time[deformation_point])
        means3D_final, scales_final, rotations_final, opacity_final, shs_final = pc._deformation(means3D, scales,
                                                                                                 rotations, opacity,
                                                                                                 shs,
                                                                                                 time)
    else:
        raise NotImplementedError

    # time2 = get_time()
    # print("asset value:",time2-time1)
    scales_final = pc.scaling_activation(scales_final)
    rotations_final = pc.rotation_activation(rotations_final)
    opacity = pc.opacity_activation(opacity_final)

    # print(opacity.max())
    # If precomputed colors are provided, use them. Otherwise, if it is desired to precompute colors
    # from SHs in Python, do it. If not, then SH -> RGB conversion will be done by rasterizer.
    # shs = None
    colors_precomp = None
    if override_color is None:
        if pipe.convert_SHs_python:
            shs_view = pc.get_features.transpose(1, 2).view(-1, 3, (pc.max_sh_degree + 1) ** 2)
            dir_pp = (pc.get_xyz - viewpoint_camera.camera_center.cuda().repeat(pc.get_features.shape[0], 1))
            dir_pp_normalized = dir_pp / dir_pp.norm(dim=1, keepdim=True)
            sh2rgb = eval_sh(pc.active_sh_degree, shs_view, dir_pp_normalized)
            colors_precomp = torch.clamp_min(sh2rgb + 0.5, 0.0)
        else:
            pass
            # shs =
    else:
        colors_precomp = override_color

    # Rasterize visible Gaussians to image, obtain their radii (on screen).
    # time3 = get_time()
    rendered_image, radii, depth = rasterizer(
        means3D=means3D_final,
        means2D=means2D,
        shs=shs_final,
        colors_precomp=colors_precomp,
        opacities=opacity,
        scales=scales_final,
        rotations=rotations_final,
        cov3D_precomp=cov3D_precomp)
    # time4 = get_time()
    # print("rasterization:",time4-time3)
    # breakpoint()
    # Those Gaussians that were frustum culled or had a radius of 0 were not visible.
    # They will be excluded from value updates used in the splitting criteria.
    return {"render": rendered_image,
            "viewspace_points": screenspace_points,
            "visibility_filter": radii > 0,
            "radii": radii,
            "depth": depth}


def render_no_train(viewpoint_camera, pc: GaussianModel, pipe, bg_color: torch.Tensor, scaling_modifier=1.0,
                    stage="fine", cam_type=None, cams_pc=None, show_radius=2.):
    """
    Render the scene outside of training GS representation of camera modesl

    """

    # TODO: add dimensions for screenspace_points for additional gaussian representation
    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0

    # Set up rasterization configuration
    means3D = pc.get_xyz

    tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
    tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)
    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=bg_color,
        scale_modifier=scaling_modifier,
        viewmatrix=viewpoint_camera.world_view_transform.cuda(),
        projmatrix=viewpoint_camera.full_proj_transform.cuda(),
        sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center.cuda(),
        prefiltered=False,
        debug=pipe.debug
    )
    
    time = torch.tensor(viewpoint_camera.time).to(means3D.device).repeat(means3D.shape[0], 1)

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)

    # Need to add to opacity and shs features (though maybe we can inject gaussians later)
    means2D = screenspace_points
    opacity = pc._opacity
    shs = pc.get_features

    # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
    # scaling / rotation by the rasterizer.
    scales = None
    rotations = None

    # Removed pre-computed cov3D TODO: See if we can precompute maybe with seperate PC class for injection
    # if pipe.compute_cov3D_python:
    #     cov3D_precomp = pc.get_covariance(scaling_modifier)
    # else:
    scales = pc._scaling
    rotations = pc._rotation

    # Commented out line below because it doesnt seem to be used
    # deformation_point = pc._deformation_table
    if "coarse" in stage:
        means3D_final, scales_final, rotations_final, opacity_final, shs_final = means3D, scales, rotations, opacity, shs
    elif "fine" in stage:
        means3D_final, scales_final, rotations_final, opacity_final, shs_final = pc._deformation(means3D, scales,
                                                                                                 rotations, opacity,
                                                                                                 shs,
                                                                                                 time)
    else:
        raise NotImplementedError

    # Recover final scales, rotations and opacities
    scales_final = pc.scaling_activation(scales_final)
    rotations_final = pc.rotation_activation(rotations_final)
    opacity = pc.opacity_activation(opacity_final)

    # We don't need this
    cov3D_precomp = None
    colors_precomp = None
        

    distances = torch.norm(means3D_final, dim=1)
    # Create a mask for the bounding box
    mask =  (distances < show_radius)

    means3D_final = means3D_final[mask]
    means2D = means2D[mask]
    shs_final = shs_final[mask]
    opacity = opacity[mask]
    scales_final = scales_final[mask]
    rotations_final = rotations_final[mask]
    
    # TODO: add shs functionality
    # shs_additional = torch.zeros_like(xyzs).to(xyzs.device).unsqueeze(1).repeat(1, 16, 1)
    if cams_pc != None:
        if cams_pc['show_cameras'] == True:
            # Construct camera model data
            xyzs = cams_pc['xyzs'].to(means3D_final.device) + 0.
            rotations_final_ = cams_pc['qs'].to(means3D_final.device) + 0.

            shs_final_ = torch.zeros_like(xyzs, requires_grad=True).to(xyzs.device).unsqueeze(1).repeat(1,
                                                                                                        shs_final.shape[
                                                                                                            1], 1) + 0.

            opacity_ = torch.ones_like(xyzs[:, 0], requires_grad=True).to(opacity.device).unsqueeze(-1) + 0.

            scaling = torch.zeros_like(xyzs, requires_grad=True).to(opacity.device) + 0.
            scaling[:, 0] = scaling[:, 0] + cams_pc['scale'][0]
            scaling[:, 1] = scaling[:, 1] + cams_pc['scale'][1]
            scaling[:, 2] = scaling[:, 2] + cams_pc['scale'][2]

            scales_final_ = scaling

            means2D_ = torch.zeros_like(xyzs, dtype=xyzs.dtype, requires_grad=True, device="cuda") + 0
            means3D_final_ = xyzs

            # To display scene and cameras at the same time
            if cams_pc['show_scene'] == True:
                means2D = torch.cat([means2D, means2D_], dim=0)
                means3D_final = torch.cat([means3D_final, means3D_final_], dim=0)
                scales_final = torch.cat([scales_final, scales_final_], dim=0)
                opacity = torch.cat([opacity, opacity_], dim=0)
                shs_final = torch.cat([shs_final, shs_final_], dim=0)
                rotations_final = torch.cat([rotations_final, rotations_final_], dim=0)
            else:  # Otherwise show cameras alone
                means2D = means2D_
                means3D_final = means3D_final_
                scales_final = scales_final_
                opacity = opacity_
                shs_final = shs_final_
                rotations_final = rotations_final_

    # Rasterize visible Gaussians to image, obtain their radii (on screen).
    # time3 = get_time()
    rendered_image, radii, depth = rasterizer(
        means3D=means3D_final,
        means2D=means2D,
        shs=shs_final,
        colors_precomp=colors_precomp,
        opacities=opacity,
        scales=scales_final,
        rotations=rotations_final,
        cov3D_precomp=cov3D_precomp)
    
    
    return {"render": rendered_image,
            "viewspace_points": screenspace_points,
            "visibility_filter": radii > 0,
            "radii": radii,
            "depth": depth}






def deform_gs(time, pc: GaussianModel, stage="fine"):
    """
    Render the scene.

    Background tensor (bg_color) must be on GPU!
    """

    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        screenspace_points.retain_grad()
    except:
        pass

    # Set up rasterization configuration
    means3D = pc.get_xyz

    time = torch.tensor(time).to(means3D.device).repeat(means3D.shape[0], 1)

    means2D = screenspace_points
    opacity = pc._opacity
    shs = pc.get_features

    scales = pc._scaling
    rotations = pc._rotation

    if "coarse" in stage:
        means3D_final, scales_final, rotations_final, opacity_final, shs_final = means3D, scales, rotations, opacity, shs
    elif "fine" in stage:
        means3D_final, scales_final, rotations_final, opacity_final, shs_final = pc._deformation(means3D, scales,
                                                                                                 rotations, opacity,
                                                                                                 shs,
                                                                                                 time)
    else:
        raise NotImplementedError

    return means3D_final
