import os
import dicom2nifti
import dicom2nifti.settings as settings

def batch_convert(root_dicom_dir, output_nifti_dir):
    """
    Search recursively for all directories containing DICOM files
    and convert them to NIfTI format in output_nifti_dir.
    """
    # Create the output directory if it doesn't exist
    os.makedirs(output_nifti_dir, exist_ok=True)
    
    # Optional: configure dicom2nifti settings
    settings.disable_validate_slice_increment() # Often necessary for clinical data

    for root, dirs, files in os.walk(root_dicom_dir):
        # We assume a directory contains a DICOM series if it has any .dcm files
        # Alternatively, if there are files and it's not the root itself
        # This simple check looks for any files that might be dicom.
        dcm_files = [f for f in files if f.endswith('.dcm') or '.' not in f]
        if len(dcm_files) > 5: # Need a minimum threshold to consider it a volume
            
            # Create a subfolder in the output based on the relative path
            rel_path = os.path.relpath(root, root_dicom_dir)
            out_folder = os.path.join(output_nifti_dir, rel_path)
            os.makedirs(out_folder, exist_ok=True)
            
            print(f"Converting Series in: {root}")
            try:
                # convert_directory writes a .nii.gz file inside out_folder automatically
                dicom2nifti.convert_directory(root, out_folder, compression=True, reorient=True)
                print(f"Success -> {out_folder}")
            except Exception as e:
                print(f"FAILED to convert {root}: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Batch DICOM to NIfTI Converter")
    parser.add_argument('dicom_dir', help="Root directory containing DICOM series")
    parser.add_argument('nifti_dir', help="Output directory for NIfTI files")
    args = parser.parse_args()
    
    batch_convert(args.dicom_dir, args.nifti_dir)
