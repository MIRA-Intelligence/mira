#!/usr/bin/env python3
"""
Basic script demonstrating loading, processing, and saving NIfTI images using nibabel.
"""

import argparse
import nibabel as nib
import numpy as np
from pathlib import Path

def process_nifti(input_path: str, output_path: str):
    """
    Loads a NIfTI image, applies a rough threshold (mock processing),
    and saves the result back to disk while preserving the affine and header.
    """
    input_file = Path(input_path)
    
    if not input_file.exists():
        raise FileNotFoundError(f"Cannot find {input_path}")
        
    print(f"Loading {input_file}...")
    img = nib.load(str(input_file))
    
    # Print metadata
    print(f"Original shape: {img.shape}")
    print(f"Voxel size (zooms): {img.header.get_zooms()}")
    
    # 1. Extract data array
    data = img.get_fdata()
    
    # 2. Perform some mock processing (e.g., simple thresholding mask)
    print("Processing array (applying threshold > 100)...")
    mask_data = (data > 100).astype(np.uint8)
    
    # 3. Create a new image using the same affine and header
    # For integer masks, using nib.Nifti1Image is standard
    print(f"Saving processed mask to {output_path}...")
    new_img = nib.Nifti1Image(mask_data, img.affine, img.header)
    
    # Update data type in header since we changed from float to uint8
    new_img.set_data_dtype(np.uint8)
    
    # Save to disk
    nib.save(new_img, str(output_path))
    print("Done!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nibabel Basic I/O")
    parser.add_argument("input", help="Path to input .nii.gz")
    parser.add_argument("output", help="Path to save output .nii.gz")
    
    args = parser.parse_args()
    process_nifti(args.input, args.output)
