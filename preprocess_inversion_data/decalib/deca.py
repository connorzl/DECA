# -*- coding: utf-8 -*-
#
# Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V. (MPG) is
# holder of all proprietary rights on this computer program.
# Using this computer program means that you agree to the terms 
# in the LICENSE file included with this software distribution. 
# Any use not explicitly granted by the LICENSE is prohibited.
#
# Copyright©2019 Max-Planck-Gesellschaft zur Förderung
# der Wissenschaften e.V. (MPG). acting on behalf of its Max Planck Institute
# for Intelligent Systems. All rights reserved.
#
# For comments or questions, please email us at deca@tue.mpg.de
# For commercial licensing contact, please contact ps-license@tuebingen.mpg.de

import os, sys
import torch
import torchvision
import torch.nn.functional as F
import torch.nn as nn

import numpy as np
from time import time
from skimage.io import imread
from skimage.transform import warp
import cv2
import pickle
from .utils.renderer import SRenderY, set_rasterizer
from .models.encoders import ResnetEncoder
from .models.FLAME import FLAME, FLAMETex
from .models.decoders import Generator
from .utils import util
from .utils.rotation_converter import batch_euler2axis
from .utils.tensor_cropper import transform_points
from .datasets import datasets
from .utils.config import cfg
torch.backends.cudnn.benchmark = True
from torchvision.utils import save_image

class DECA(nn.Module):
    def __init__(self, config=None, device='cuda'):
        super(DECA, self).__init__()
        if config is None:
            self.cfg = cfg
        else:
            self.cfg = config
        self.device = device
        self.image_size = self.cfg.model.texture_image_size
        self.uv_size = self.cfg.model.uv_size
        self.uv_size_coarse = self.cfg.model.uv_size_coarse

        self._create_model(self.cfg.model)
        self._setup_renderer(self.cfg.model)

    def _setup_renderer(self, model_cfg):
        set_rasterizer(self.cfg.rasterizer_type)
        self.render = SRenderY(self.image_size, obj_filename=model_cfg.topology_path, uv_size=model_cfg.uv_size, uv_size_coarse=model_cfg.uv_size_coarse, rasterizer_type=self.cfg.rasterizer_type).to(self.device)

        # face mask for rendering details
        mask = imread(model_cfg.face_eye_mask_path).astype(np.float32)/255.; mask = torch.from_numpy(mask[:,:,0])[None,None,:,:].contiguous()
        self.uv_face_eye_mask_coarse = F.interpolate(mask, [model_cfg.uv_size_coarse, model_cfg.uv_size_coarse]).to(self.device)
        self.uv_face_eye_mask = F.interpolate(mask, [model_cfg.uv_size, model_cfg.uv_size]).to(self.device)

        mask = imread(model_cfg.face_mask_path).astype(np.float32)/255.; mask = torch.from_numpy(mask[:,:,0])[None,None,:,:].contiguous()
        self.uv_face_mask = F.interpolate(mask, [model_cfg.uv_size, model_cfg.uv_size]).to(self.device)
        # displacement correction
        fixed_dis = np.load(model_cfg.fixed_displacement_path)
        self.fixed_uv_dis = torch.tensor(fixed_dis).float().to(self.device)
        self.fixed_uv_dis = F.interpolate(self.fixed_uv_dis[None, None, ...],
                                          (self.uv_size_coarse, self.uv_size_coarse), mode='bilinear').squeeze()
        # mean texture
        mean_texture = imread(model_cfg.mean_tex_path).astype(np.float32)/255.; mean_texture = torch.from_numpy(mean_texture.transpose(2,0,1))[None,:,:,:].contiguous()
        self.mean_texture = F.interpolate(mean_texture, [model_cfg.uv_size, model_cfg.uv_size]).to(self.device)
        # dense mesh template, for save detail mesh
        self.dense_template = np.load(model_cfg.dense_template_path, allow_pickle=True, encoding='latin1').item()

    def _create_model(self, model_cfg):
        # set up parameters
        self.n_param = model_cfg.n_shape+model_cfg.n_tex+model_cfg.n_exp+model_cfg.n_pose+model_cfg.n_cam+model_cfg.n_light
        self.n_detail = model_cfg.n_detail
        self.n_cond = model_cfg.n_exp + 3 # exp + jaw pose
        self.num_list = [model_cfg.n_shape, model_cfg.n_tex, model_cfg.n_exp, model_cfg.n_pose, model_cfg.n_cam, model_cfg.n_light]
        self.param_dict = {i:model_cfg.get('n_' + i) for i in model_cfg.param_list}

        # encoders
        self.E_flame = ResnetEncoder(outsize=self.n_param).to(self.device) 
        self.E_detail = ResnetEncoder(outsize=self.n_detail).to(self.device)
        # decoders
        self.flame = FLAME(model_cfg).to(self.device)
        if model_cfg.use_tex:
            self.flametex = FLAMETex(model_cfg).to(self.device)
        self.D_detail = Generator(latent_dim=self.n_detail+self.n_cond, out_channels=1, out_scale=model_cfg.max_z, sample_mode = 'bilinear', uv_size=self.cfg.model.uv_size_coarse).to(self.device)
        # resume model
        model_path = self.cfg.pretrained_modelpath
        if os.path.exists(model_path):
            print(f'trained model found. load {model_path}')
            checkpoint = torch.load(model_path)
            self.checkpoint = checkpoint
            util.copy_state_dict(self.E_flame.state_dict(), checkpoint['E_flame'])
            util.copy_state_dict(self.E_detail.state_dict(), checkpoint['E_detail'])
            util.copy_state_dict(self.D_detail.state_dict(), checkpoint['D_detail'])
        else:
            print(f'please check model path: {model_path}')
            # exit()
        # eval mode
        self.E_flame.eval()
        self.E_detail.eval()
        self.D_detail.eval()

    def decompose_code(self, code, num_dict):
        ''' Convert a flattened parameter vector to a dictionary of parameters
        code_dict.keys() = ['shape', 'tex', 'exp', 'pose', 'cam', 'light']
        '''
        code_dict = {}
        start = 0
        for key in num_dict:
            end = start+int(num_dict[key])
            code_dict[key] = code[:, start:end]
            start = end
            if key == 'light':
                code_dict[key] = code_dict[key].reshape(code_dict[key].shape[0], 9, 3)
        return code_dict

    def displacement2normal(self, uv_z, coarse_verts, coarse_normals):
        ''' Convert displacement map into detail normal map
        '''
        batch_size = uv_z.shape[0]
        uv_coarse_vertices = self.render.world2uv(coarse_verts, coarse=True).detach()
        uv_coarse_normals = self.render.world2uv(coarse_normals, coarse=True).detach()

        uv_z = uv_z*self.uv_face_eye_mask_coarse
        uv_detail_vertices = uv_coarse_vertices + uv_z*uv_coarse_normals + self.fixed_uv_dis[None,None,:,:]*uv_coarse_normals.detach()

        dense_vertices = uv_detail_vertices.permute(0,2,3,1).reshape([batch_size, -1, 3])
        uv_detail_normals = util.vertex_normals(dense_vertices, self.render.dense_faces.expand(batch_size, -1, -1))
        uv_detail_normals = uv_detail_normals.reshape([batch_size, uv_coarse_vertices.shape[2], uv_coarse_vertices.shape[3], 3]).permute(0,3,1,2)

        uv_detail_normals = uv_detail_normals*self.uv_face_eye_mask_coarse + uv_coarse_normals*(1-self.uv_face_eye_mask_coarse)
        uv_detail_normals = F.interpolate(uv_detail_normals, (self.uv_size, self.uv_size))

        return uv_detail_normals

    def visofp(self, normals):
        ''' visibility of keypoints, based on the normal direction
        '''
        normals68 = self.flame.seletec_3d68(normals)
        vis68 = (normals68[:,:,2:] < 0.1).float()
        return vis68

    # @torch.no_grad()
    def encode(self, images, use_detail=True):
        if use_detail:
            # use_detail is for training detail model, need to set coarse model as eval mode
            with torch.no_grad():
                parameters = self.E_flame(images)
        else:
            parameters = self.E_flame(images)
        codedict = self.decompose_code(parameters, self.param_dict)
        codedict['images'] = images
        if use_detail:
            detailcode = self.E_detail(images)
            codedict['detail'] = detailcode
        if self.cfg.model.jaw_type == 'euler':
            posecode = codedict['pose']
            euler_jaw_pose = posecode[:,3:].clone() # x for yaw (open mouth), y for pitch (left ang right), z for roll
            posecode[:,3:] = batch_euler2axis(euler_jaw_pose)
            codedict['pose'] = posecode
            codedict['euler_jaw_pose'] = euler_jaw_pose  
        return codedict

    def decode_coarse(self, codedict, rendering=True, iddict=None, vis_lmk=True, return_vis=True, use_detail=True,
                render_orig=False, original_image=None, tform=None, pca_index=0, pca_scale=1, all_scale=1, freeze_eyes=None):
        images = codedict['images']
        hr_images = codedict['hr_images']
        batch_size = images.shape[0]
        
        ## decode
        verts, landmarks2d, landmarks3d, freeze_eyes = self.flame(shape_params=codedict['shape'], expression_params=codedict['exp'], pose_params=codedict['pose'], pca_index=pca_index, pca_scale=pca_scale, all_scale=all_scale, freeze_eyes=freeze_eyes)
        if self.cfg.model.use_tex:
            albedo = self.flametex(codedict['tex'])
        else:
            albedo = torch.zeros([batch_size, 3, self.uv_size, self.uv_size], device=images.device) 
        landmarks3d_world = landmarks3d.clone()

        ## projection
        trans_verts = util.batch_orth_proj(verts, codedict['cam']); trans_verts[:,:,1:] = -trans_verts[:,:,1:]

        opdict = {
            'verts': verts,
            'trans_verts': trans_verts,
            'freeze_eyes': freeze_eyes,
        }

        ## rendering
        if rendering:
            uv_z = self.D_detail(torch.cat([codedict['pose'][:,3:], codedict['exp'], codedict['detail']], dim=1))
            if iddict is not None:
                uv_z = self.D_detail(torch.cat([iddict['pose'][:,3:], iddict['exp'], codedict['detail']], dim=1))

            normals = util.vertex_normals(verts, self.render.faces.expand(batch_size, -1, -1))
            uv_detail_normals = self.displacement2normal(uv_z, verts, normals)
            uv_shading = self.render.add_SHlight(uv_detail_normals, codedict['light'])
            uv_texture = albedo*uv_shading

            opdict['uv_texture'] = uv_texture 
            opdict['normals'] = normals
            opdict['uv_detail_normals'] = uv_detail_normals
            opdict['displacement_map'] = uv_z+self.fixed_uv_dis[None,None,:,:]

            if 'attributes' in codedict:
                uv_pverts = self.render.world2uv(trans_verts, attributes=codedict['attributes'])
            else:
                uv_pverts = self.render.world2uv(trans_verts)
                # store attributes for transfer
                face_vertices = util.face_vertices(trans_verts, self.render.faces.expand(trans_verts.shape[0], -1, -1))
                opdict['attributes'] = face_vertices

            uv_gt = F.grid_sample(hr_images, uv_pverts.permute(0,2,3,1)[:,:,:,:2], mode='bilinear', align_corners=False)
            uv_gt_mask = F.grid_sample(torch.ones_like(hr_images), uv_pverts.permute(0,2,3,1)[:,:,:,:2], mode='bilinear', align_corners=False)[:, [0], ...]

            if self.cfg.model.use_tex:
                # inpaint any missing texture regions
                # uv_gt[uv_gt_mask==0] = uv_texture[uv_gt_mask==0]
                uv_face_eye_mask = self.uv_face_eye_mask * uv_gt_mask
                # combined
                uv_texture_gt = uv_gt[:,:3,:,:]*uv_face_eye_mask + (uv_texture[:,:3,:,:]*(1-uv_face_eye_mask))
            else:
                uv_face_eye_mask = self.uv_face_eye_mask * uv_gt_mask
                uv_texture_gt = uv_gt[:,:3,:,:]
            opdict['uv_texture_gt'] = uv_texture_gt

        if return_vis:
            if render_orig and original_image is not None and tform is not None:
                points_scale = [self.image_size, self.image_size]
                _, _, h, w = original_image.shape
                trans_verts = transform_points(trans_verts, tform, points_scale, [h, w])
                background = original_image
                images = original_image
            else:
                print("no tform available")
                h, w = self.image_size, self.image_size
                background = None

            # Render the coarse mesh using the texture map.
            ops = self.render(verts, trans_verts, uv_texture_gt, None, h=h, w=w, bg_images=background, face_mask=uv_face_eye_mask)
            opdict['trans_verts'] = trans_verts
            visdict = {
                'inputs': images,
                'mask': ops['mask'].repeat(1, 3, 1, 1),
                'rendered_images': ops['images']
            }
            return opdict, visdict

    # @torch.no_grad()
    def decode(self, codedict, rendering=True, iddict=None, vis_lmk=True, return_vis=True, use_detail=True,
                render_orig=False, original_image=None, tform=None):
        images = codedict['images']
        hr_images = codedict['hr_images']
        batch_size = images.shape[0]
        
        ## decode
        verts, landmarks2d, landmarks3d, _ = self.flame(shape_params=codedict['shape'], expression_params=codedict['exp'], pose_params=codedict['pose'])
        if self.cfg.model.use_tex:
            albedo = self.flametex(codedict['tex'])
        else:
            albedo = torch.zeros([batch_size, 3, self.uv_size, self.uv_size], device=images.device) 
        landmarks3d_world = landmarks3d.clone()

        ## projection
        landmarks2d = util.batch_orth_proj(landmarks2d, codedict['cam'])[:,:,:2]; landmarks2d[:,:,1:] = -landmarks2d[:,:,1:]#; landmarks2d = landmarks2d*self.image_size/2 + self.image_size/2
        landmarks3d = util.batch_orth_proj(landmarks3d, codedict['cam']); landmarks3d[:,:,1:] = -landmarks3d[:,:,1:] #; landmarks3d = landmarks3d*self.image_size/2 + self.image_size/2
        trans_verts = util.batch_orth_proj(verts, codedict['cam']); trans_verts[:,:,1:] = -trans_verts[:,:,1:]

        opdict = {
            'verts': verts,
            'trans_verts': trans_verts,
            'landmarks2d': landmarks2d,
            'landmarks3d': landmarks3d,
            'landmarks3d_world': landmarks3d_world,
        }

        ## rendering
        if rendering:
            uv_z = self.D_detail(torch.cat([codedict['pose'][:,3:], codedict['exp'], codedict['detail']], dim=1))
            if iddict is not None:
                uv_z = self.D_detail(torch.cat([iddict['pose'][:,3:], iddict['exp'], codedict['detail']], dim=1))

            normals = util.vertex_normals(verts, self.render.faces.expand(batch_size, -1, -1))
            uv_detail_normals = self.displacement2normal(uv_z, verts, normals)
            uv_shading = self.render.add_SHlight(uv_detail_normals, codedict['light'])
            uv_texture = albedo*uv_shading

            opdict['uv_texture'] = uv_texture 
            opdict['normals'] = normals
            opdict['uv_detail_normals'] = uv_detail_normals
            opdict['displacement_map'] = uv_z+self.fixed_uv_dis[None,None,:,:]

            if 'attributes' in codedict:
                uv_pverts = self.render.world2uv(trans_verts, attributes=codedict['attributes'])
            else:
                uv_pverts = self.render.world2uv(trans_verts)
                # store attributes for transfer
                face_vertices = util.face_vertices(trans_verts, self.render.faces.expand(trans_verts.shape[0], -1, -1))
                opdict['attributes'] = face_vertices

            uv_gt = F.grid_sample(hr_images, uv_pverts.permute(0,2,3,1)[:,:,:,:2], mode='bilinear', align_corners=False)
            uv_gt_mask = F.grid_sample(torch.ones_like(hr_images), uv_pverts.permute(0,2,3,1)[:,:,:,:2], mode='bilinear', align_corners=False)

            if self.cfg.model.use_tex:
                # inpaint any missing texture regions
                # uv_gt[uv_gt_mask==0] = uv_texture[uv_gt_mask==0]
                uv_face_eye_mask = self.uv_face_eye_mask * uv_gt_mask
                # combined
                uv_texture_gt = uv_gt[:,:3,:,:]*uv_face_eye_mask + (uv_texture[:,:3,:,:]*(1-uv_face_eye_mask))
            else:
                uv_face_eye_mask = self.uv_face_eye_mask * uv_gt_mask
                uv_texture_gt = uv_gt[:,:3,:,:]

            opdict['uv_texture_gt'] = uv_texture_gt
            save_image(uv_texture_gt[0].cpu(), "./coarse_uv_texture.png")
        
        if self.cfg.model.use_tex:
            opdict['albedo'] = albedo

        if return_vis:
            if render_orig and original_image is not None and tform is not None:
                points_scale = [self.image_size, self.image_size]
                _, _, h, w = original_image.shape
                # import ipdb; ipdb.set_trace()
                trans_verts = transform_points(trans_verts, tform, points_scale, [h, w])
                landmarks2d = transform_points(landmarks2d, tform, points_scale, [h, w])
                landmarks3d = transform_points(landmarks3d, tform, points_scale, [h, w])
                background = original_image
                images = original_image
            else:
                print("no tform available")
                h, w = self.image_size, self.image_size
                background = None

            # Render the coarse mesh using the texture map.
            ops = self.render(verts, trans_verts, uv_texture_gt, None, h=h, w=w, bg_images=background, face_mask=uv_face_eye_mask)
            
            ## output
            opdict['grid'] = ops['grid']
            opdict['rendered_images'] = ops['images']
            opdict['alpha_images'] = ops['alpha_images']
            opdict['normal_images'] = ops['normal_images']

            if vis_lmk:
                landmarks3d_vis = self.visofp(ops['transformed_normals'])#/self.image_size
                landmarks3d = torch.cat([landmarks3d, landmarks3d_vis], dim=2)
                opdict['landmarks3d'] = landmarks3d

            ## render shape
            shape_images, _, grid, alpha_images = self.render.render_shape(verts, trans_verts, h=h, w=w, images=background, return_grid=True)

            detail_normal_images = F.grid_sample(uv_detail_normals, grid, align_corners=False)*alpha_images
            shape_detail_images = self.render.render_shape(verts, trans_verts, detail_normal_images=detail_normal_images, h=h, w=w, images=background)

            # Render the detailed mesh.
            vertices = opdict['verts'][0].cpu().numpy()
            faces = self.render.faces[0].cpu().numpy()
            texture = util.tensor2image(opdict['uv_texture_gt'][0])
            texture = texture[:,:,[2,1,0]]
            normals = opdict['normals'][0].cpu().numpy()

            displacement_map = opdict['displacement_map'][0].cpu().detach().numpy().squeeze()
            dense_vertices, dense_colors, dense_faces, dense_uvcoords, dense_uvfaces = util.upsample_mesh(vertices, normals, faces, displacement_map, texture, self.dense_template)

            # Normalize UV coordinates.
            dense_uvcoords = torch.from_numpy(dense_uvcoords).cuda().unsqueeze(0) / np.max(dense_uvcoords)
            dense_uvcoords = torch.cat([dense_uvcoords, dense_uvcoords[:,:,0:1]*0.+1.], -1) #[bz, ntv, 3]
            dense_uvcoords = dense_uvcoords*2 - 1
            dense_uvfaces = torch.from_numpy(dense_uvfaces).cuda().unsqueeze(0)

            # Transform vertices.
            dense_vertices = torch.from_numpy(dense_vertices.astype(np.float32)).unsqueeze(0).cuda()
            dense_trans_verts = util.batch_orth_proj(dense_vertices, codedict['cam'])
            dense_trans_verts[:,:,1:] = -dense_trans_verts[:,:,1:]
            dense_faces = torch.from_numpy(dense_faces).cuda().unsqueeze(0)

            detail_normal_images = F.grid_sample(uv_detail_normals, grid, align_corners=False)*alpha_images
            detail_face_uvcoords = util.face_vertices(dense_uvcoords, dense_uvfaces)

            # tried this instead of the above code for shape_detail_images, but doesn't seem to matter
            # shape_detail_images, _, _, _ = self.render.render_shape(dense_vertices, dense_trans_verts,
            #                                                         h=h, w=w, images=background, return_grid=True,
            #                                                         detail_normal_images=detail_normal_images,
            #                                                         detail_faces=dense_faces,
            #                                                         detail_face_uvcoords=detail_face_uvcoords)

            visdict = {
                'hr_inputs': hr_images,
                'landmarks2d': util.tensor_vis_landmarks(images, landmarks2d),
                #'landmarks3d': util.tensor_vis_landmarks(images, landmarks3d),
                'shape_images': shape_images,
                'shape_detail_images': shape_detail_images,
                'mask': ops['mask'].repeat(1, 3, 1, 1)
            }
            # if self.cfg.model.use_tex:
            visdict['rendered_images'] = ops['images']
            visdict['mask'] = ops['mask']


            if 'dense_attributes' in codedict:
                uv_pverts = self.render.world2uv_dense(dense_trans_verts, dense_faces, dense_uvcoords, dense_uvfaces, attributes=codedict['dense_attributes'], debug=True)
            else:
                uv_pverts = self.render.world2uv_dense(dense_trans_verts, dense_faces, dense_uvcoords, dense_uvfaces, debug=True)
                # store attributes for transfer
                dense_face_vertices = util.face_vertices(dense_trans_verts, dense_faces.expand(dense_vertices.shape[0], -1, -1))
                opdict['dense_attributes'] = dense_face_vertices

            uv_gt = F.grid_sample(hr_images, uv_pverts.permute(0,2,3,1)[:,:,:,:2], mode='bilinear', align_corners=False)
            uv_gt_mask = F.grid_sample(torch.ones_like(hr_images), uv_pverts.permute(0,2,3,1)[:,:,:,:2], mode='bilinear', align_corners=False)

            if self.cfg.model.use_tex:
                # inpaint any missing texture regions
                # uv_gt[uv_gt_mask==0] = uv_texture[uv_gt_mask==0]
                uv_face_eye_mask = self.uv_face_eye_mask * uv_gt_mask 
                uv_texture_gt = uv_gt[:,:3,:,:]*uv_face_eye_mask + (uv_texture[:,:3,:,:]*(1-uv_face_eye_mask))
            else:
                uv_face_eye_mask = self.uv_face_eye_mask * uv_gt_mask
                uv_texture_gt = uv_gt[:,:3,:,:]
            save_image(uv_texture_gt[0].cpu(), "./dense_uv_texture.png")

            if render_orig and original_image is not None and tform is not None:
                points_scale = [self.image_size, self.image_size]
                _, _, h, w = original_image.shape
                dense_trans_verts = transform_points(dense_trans_verts, tform, points_scale, [h, w])

            ops = self.render.render_dense(dense_vertices, dense_faces, util.face_vertices(dense_uvcoords, dense_uvfaces), dense_trans_verts, uv_texture_gt, None, h=h, w=w, bg_images=background, face_mask=uv_face_eye_mask)
            visdict['rendered_images_detailed'] = ops['images']
            visdict['mask_detailed'] = ops['mask']

            # import matplotlib.pyplot as plt
            # plt.subplot(141)
            # plt.imshow(uv_detail_normals.squeeze().permute(1, 2, 0).cpu().numpy())
            # plt.subplot(142)
            # plt.imshow(detail_normal_images.squeeze().permute(1, 2, 0).cpu().numpy())
            # plt.subplot(143)
            # plt.imshow(shape_detail_images.squeeze().cpu().permute(1, 2, 0).numpy())
            # plt.subplot(144)
            # plt.show()

            return opdict, visdict

        else:
            return opdict

    def visualize(self, visdict, size=224, dim=2):
        '''
        image range should be [0,1]
        dim: 2 for horizontal. 1 for vertical
        '''
        assert dim == 1 or dim==2
        grids = {}
        for key in visdict:
            _,_,h,w = visdict[key].shape
            if dim == 2:
                new_h = size; new_w = int(w*size/h)
            elif dim == 1:
                new_h = int(h*size/w); new_w = size
            grids[key] = torchvision.utils.make_grid(F.interpolate(visdict[key], [new_h, new_w]).detach().cpu())
        grid = torch.cat(list(grids.values()), dim)
        grid_image = (grid.numpy().transpose(1,2,0).copy()*255)[:,:,[2,1,0]]
        grid_image = np.minimum(np.maximum(grid_image, 0), 255).astype(np.uint8)
        return grid_image
    
    def save_obj(self, filename, opdict):
        '''
        vertices: [nv, 3], tensor
        texture: [3, h, w], tensor
        '''
        i = 0
        vertices = opdict['verts'][i].cpu().numpy()
        faces = self.render.faces[0].cpu().numpy()
        texture = util.tensor2image(opdict['uv_texture_gt'][i])
        uvcoords = self.render.raw_uvcoords[0].cpu().numpy()
        uvfaces = self.render.uvfaces[0].cpu().numpy()
        # save coarse mesh, with texture and normal map
        normal_map = util.tensor2image(opdict['uv_detail_normals'][i]*0.5 + 0.5)
        util.write_obj(filename, vertices, faces, 
                        texture=texture, 
                        uvcoords=uvcoords, 
                        uvfaces=uvfaces, 
                        normal_map=normal_map)
        """
        # upsample mesh, save detailed mesh
        texture = texture[:,:,[2,1,0]]
        normals = opdict['normals'][i].cpu().numpy()
        displacement_map = opdict['displacement_map'][i].cpu().detach().numpy().squeeze()
        dense_vertices, dense_colors, dense_faces, _, _ = util.upsample_mesh(vertices, normals, faces, displacement_map, texture, self.dense_template)
        util.write_obj(filename.replace('.obj', '_detail.obj'), 
                        dense_vertices, 
                        dense_faces,
                        #colors = dense_colors,
                        inverse_face_order=True)
        """
    def run(self, imagepath, iscrop=True):
        ''' An api for running deca given an image path
        '''
        testdata = datasets.TestData(imagepath)
        images = testdata[0]['image'].to(self.device)[None,...]
        codedict = self.encode(images)
        opdict, visdict = self.decode(codedict)
        return codedict, opdict, visdict

    def model_dict(self):
        return {
            'E_flame': self.E_flame.state_dict(),
            'E_detail': self.E_detail.state_dict(),
            'D_detail': self.D_detail.state_dict()
        }
