# 3D GAN Inversion for Controllable Portrait Image Animation

This is the official PyTorch implementation of [3D GAN Inversion for Controllable Portrait Image Animation](https://arxiv.org/abs/2203.13441), which uses modified implementations of [DECA: Detailed Expression Capture and Animation](https://github.com/yfeng95/DECA) and [PTI: Pivotal Tuning for Latent-based editing of Real Images](https://github.com/danielroich/PTI).

## Getting Started
Clone the repo:
  ```bash
  git clone https://github.com/connorzl/DECA
  cd DECA
  ```  

### Requirements
* DECA environment  
  ```
  conda env create --file=environment.yml
  ```
* pytorch3d
  ```
  git clone https://github.com/facebookresearch/pytorch3d.git
  cd pytorch3d && pip install -e . 
  ```

### Usage
1. Prepare data   
    a. download [FLAME model](https://flame.is.tue.mpg.de/download.php), choose **FLAME 2020** and unzip it, copy 'generic_model.pkl' into ./preprocess_inversion_data/data  
    
    b. download [DECA trained model](https://drive.google.com/file/d/1rp8kdyLPvErw2dTmqtjISRVvQLj6Yzje/view?usp=sharing), and put it in ./preprocess_inversion_data/data (**no unzip required**) 
    
    c. (Optional) follow the instructions for the [Albedo model](https://github.com/TimoBolkart/BFM_to_FLAME) to get 'FLAME_albedo_from_BFM.npz', put it into ./preprocess_inversion_data/data
    
    d. download pretrained [EG3D checkpoint and face alignment models](https://drive.google.com/drive/folders/18cUIvd0w-rnTVzeBpc-adsawiq3Njjla?usp=sharing) and put final_1600.pkl and align.dat in ./inversion/pti_inversion
    
    e. estimate camera pose extrinsics for each source and target frame input. We use [Deep3DFaceReconstruction](https://github.com/sicxu/Deep3DFaceRecon_pytorch) in our paper. See [inversion_data](https://github.com/connorzl/DECA/tree/master/inversion_data) for examples of expected input. Additional instructions and helper scripts are available [at this link](https://github.com/connorzl/eg3d_pti_inversion).

2. Preprocess data for 3D GAN Inversion:
    ```
    cd preprocess_inversion_data
    python generate_dataset.py -i ../inversion_data/source_images  -e ../inversion_data/target_images -s ../inversion_data/output --device cuda:0 
    ```   
    to visualize the predicted depth, face mask, face mask rendered source image with target expression, and fully rendered source image with target expression (see [inversion_data/output](https://github.com/connorzl/DECA/tree/master/inversion_data/output) in this repository for example outputs).   
 
3. Run 3D GAN Inversion:
  ```
  cd inversion/pti_inversion
  python run_inversion.py --experiment_name adam_voxceleb --input_data_path ../../inversion_data/output --input_pose_path ../../inversion_data/source_poses/cameras.json --logging_root logs --gpu 0
  python composite_results.py --input_logs_path logs/adam_voxceleb --input_data_path ../../inversion_data/output --input_pose_path ../../inversion_data/source_poses/cameras.json --target_pose_path ../../inversion_data/target_poses/cameras.json
  ```

## Citation
If you find this work useful to your research, please consider citing:
```
@article{lin20223d,
  title={3d gan inversion for controllable portrait image animation},
  author={Lin, Connor Z and Lindell, David B and Chan, Eric R and Wetzstein, Gordon},
  journal={arXiv preprint arXiv:2203.13441},
  year={2022}
}
```
## License
This code and model are available for non-commercial scientific research purposes as defined in the [LICENSE](https://github.com/YadiraF/DECA/blob/master/LICENSE) file.
By downloading and using the code and model you agree to the terms in the [LICENSE](https://github.com/YadiraF/DECA/blob/master/LICENSE). 
