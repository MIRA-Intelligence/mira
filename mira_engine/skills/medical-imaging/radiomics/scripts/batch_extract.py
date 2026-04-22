import os
import pandas as pd
from radiomics import featureextractor

def batch_extract(image_dir, mask_dir, output_csv, params_file=None):
    """
    Batch extract radiomics features for a cohort.
    Requires matching filenames between image_dir and mask_dir.
    """
    if params_file and os.path.exists(params_file):
        extractor = featureextractor.RadiomicsFeatureExtractor(params_file)
    else:
        # Default PyRadiomics initialization
        extractor = featureextractor.RadiomicsFeatureExtractor()

    results = []
    
    # Iterate through images
    for filename in sorted(os.listdir(image_dir)):
        if not filename.endswith('.nii.gz'): 
            continue
            
        img_path = os.path.join(image_dir, filename)
        mask_path = os.path.join(mask_dir, filename) 
        
        if not os.path.exists(mask_path):
            print(f"Skipping {filename}: Mask not found.")
            continue
            
        print(f"Extracting features for {filename}...")
        try:
            feature_vector = extractor.execute(img_path, mask_path)
            
            # Clean up PyRadiomics dictionary (removing nested structures)
            row = {'PatientID': filename}
            for key, value in feature_vector.items():
                if not key.startswith('diagnostics_'):
                    row[key] = value
                    
            results.append(row)
        except Exception as e:
            print(f"Failed on {filename}: {e}")
            
    df = pd.DataFrame(results)
    df.to_csv(output_csv, index=False)
    print(f"Successfully saved features to {output_csv}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Radiomics Batch Extractor")
    parser.add_argument('--image_dir', required=True, help="Path to NIfTI images")
    parser.add_argument('--mask_dir', required=True, help="Path to NIfTI masks")
    parser.add_argument('--out_csv', required=True, help="Output CSV path")
    parser.add_argument('--params', default=None, help="Path to PyRadiomics YAML params")
    args = parser.parse_args()
    
    batch_extract(args.image_dir, args.mask_dir, args.out_csv, args.params)
