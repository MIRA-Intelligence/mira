# MONAI Networks

## Overview
MONAI provides 1D, 2D, and 3D network architectures tailored to medical imagery.

## Common Architectures
- **UNet**: The standard configurable U-Net. Use for simple baselines.
- **SwinUNETR**: Transformer-based encoder with a U-Net like decoder. State-of-the-Art for multi-modal brain segmentation (BraTS) and multi-organ segmentation.
- **SegResNet**: Residual network with asymmetric encoder-decoder. Highly competitive, especially for Brain Tumors.
- **VNet**: Fully convolutional neural network designed for volumetric medical image segmentation.

## Instantiation
Ensure `spatial_dims` matches your data (e.g., `spatial_dims=3` for volumes). Determine `in_channels` and `out_channels` based precisely on pipeline planning.
