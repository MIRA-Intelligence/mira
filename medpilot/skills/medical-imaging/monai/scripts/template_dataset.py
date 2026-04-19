import os
from monai.data import CacheDataset, DataLoader
from template_transforms import get_train_transforms, get_val_transforms

def build_data_loaders(data_dir, batch_size=2, num_workers=4):
    """
    Scans a directory containing 'images' and 'masks' folders,
    constructs data dictionaries, and builds caching dataloaders.
    """
    images_dir = os.path.join(data_dir, "images")
    masks_dir = os.path.join(data_dir, "masks")
    
    # Assume 1-to-1 matching via sorted files
    images = sorted([os.path.join(images_dir, f) for f in os.listdir(images_dir) if f.endswith('.nii.gz')])
    labels = sorted([os.path.join(masks_dir, f) for f in os.listdir(masks_dir) if f.endswith('.nii.gz')])
    
    data_dicts = [{"image": img, "label": lbl} for img, lbl in zip(images, labels)]
    
    # Very naive split (80/20)
    split_idx = int(len(data_dicts) * 0.8)
    train_files, val_files = data_dicts[:split_idx], data_dicts[split_idx:]
    
    print(f"Caching {len(train_files)} Training Volumes...")
    train_ds = CacheDataset(
        data=train_files, 
        transform=get_train_transforms(),
        cache_rate=1.0, 
        num_workers=num_workers
    )
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=num_workers)

    print(f"Caching {len(val_files)} Validation Volumes...")
    val_ds = CacheDataset(
        data=val_files, 
        transform=get_val_transforms(),
        cache_rate=1.0, 
        num_workers=num_workers
    )
    val_loader = DataLoader(val_ds, batch_size=1, shuffle=False, num_workers=num_workers)
    
    return train_loader, val_loader
