from monai.transforms import (
    Compose,
    LoadImaged,
    EnsureChannelFirstd,
    Spacingd,
    Orientationd,
    ScaleIntensityRanged,
    RandCropByPosNegLabeld,
    RandAffined,
    RandGaussianNoised,
    ToTensord,
)

def get_train_transforms(keys=["image", "label"]):
    """
    Standard training transform pipeline for 3D Segmentation.
    """
    return Compose([
        LoadImaged(keys=keys),
        EnsureChannelFirstd(keys=keys),
        # Assuming typical CT: resample to 1x1x1 mm
        Spacingd(keys=keys, pixdim=(1.0, 1.0, 1.0), mode=("bilinear", "nearest")),
        # Standardize matrix orientation
        Orientationd(keys=keys, axcodes="RAS"),
        # Example CT abstraction (-1000 to 400 HU into 0-1)
        ScaleIntensityRanged(
            keys=["image"], a_min=-1000, a_max=400, b_min=0.0, b_max=1.0, clip=True
        ),
        # Crop balanced patches
        RandCropByPosNegLabeld(
            keys=keys,
            label_key="label",
            spatial_size=(96, 96, 96),
            pos=1, neg=1,
            num_samples=4, # Yield 4 patches per volume
            image_key="image"
        ),
        # Augmentations
        RandAffined(
            keys=keys, mode=("bilinear", "nearest"),
            prob=0.5, spatial_size=(96, 96, 96),
            rotate_range=(0.1, 0.1, 0.1)
        ),
        RandGaussianNoised(keys=["image"], prob=0.1),
        ToTensord(keys=keys)
    ])

def get_val_transforms(keys=["image", "label"]):
    """
    Standard validation transform pipeline without random augmentations or cropping.
    (Cropping is handled by SlidingWindowInferer during inference)
    """
    return Compose([
        LoadImaged(keys=keys),
        EnsureChannelFirstd(keys=keys),
        Spacingd(keys=keys, pixdim=(1.0, 1.0, 1.0), mode=("bilinear", "nearest")),
        Orientationd(keys=keys, axcodes="RAS"),
        ScaleIntensityRanged(
            keys=["image"], a_min=-1000, a_max=400, b_min=0.0, b_max=1.0, clip=True
        ),
        ToTensord(keys=keys)
    ])
