# DeepStream YOLO Setup Guide

This guide provides step-by-step instructions for setting up and running YOLO models with NVIDIA DeepStream.

## Prerequisites

- NVIDIA GPU with CUDA support
- NVIDIA DeepStream SDK 7.1 or later
- Python 3.6 or higher
- Git

## Setup Instructions

### 1. System Update
Update your system using the provided DeepStream scripts:

```bash
# List available update scripts
ls /opt/nvidia/deepstream/deepstream-7.1/
# Run the necessary update scripts
# Example:
# sudo ./update_rtpmanager.sh
# sudo ./user_additional_install.sh
# sudo ./user_deepstream_python_apps_install.sh
```

### 2. Clone Required Repositories

```bash
# Clone DeepStream-Yolo repository
git clone https://github.com/marcoslucianops/DeepStream-Yolo.git

# Clone Ultralytics repository
git clone https://github.com/ultralytics/ultralytics.git
cd ultralytics

# Install requirements
pip3 install -e .
pip3 install onnx onnxslim onnxruntime
```

### 3. Prepare YOLO Model

1. Copy the export script:
   ```bash
   cp DeepStream-Yolo/utils/export_yolo11.py ultralytics/
   ```

2. Convert your YOLO model to ONNX format:
   ```bash
   cd ultralytics
   python3 export_yolo11.py -w path/to/your/yolo11s.pt --dynamic
   ```
   
   This will generate a `.pt.onnx` file in the current directory.

### 4. Configure DeepStream

1. Update the inference configuration file at `DeepStream-Yolo/config_infer_primary_yolo11.txt`:
   ```ini
   [property]
   # Update these paths to your model and labels
   onnx-file=path/to/your/yolo11s.pt.onnx
   model-engine-file=model_b1_gpu0_fp16.engine
   labelfile-path=labels.txt
   num-detected-classes=80
   parse-bbox-func-name=NvDsInferParseYolo
   ```

2. Update the DeepStream app configuration at `DeepStream-Yolo/deepstream_app_config.txt`:
   ```ini
   [primary-gie]
   enable=1
   gpu-id=0
   model-engine-file=model_b1_gpu0_fp16.engine
   config-file=config_infer_primary_yolo11.txt
   ```

### 5. Environment Setup

Set the CUDA version environment variable:
```bash
export CUDA_VER=12.6
```

### 6. Build the Custom YOLO Plugin

```bash
cd DeepStream-Yolo
make -C nvdsinfer_custom_impl_Yolo clean
make -C nvdsinfer_custom_impl_Yolo
```

### 7. Run the Application

```bash
cd DeepStream-Yolo
deepstream-app -c deepstream_app_config.txt
```

## Troubleshooting

- **CUDA Version Mismatch**: Ensure `CUDA_VER` is set to 12.6.
- **Model Conversion Issues**: Verify your YOLO model is compatible with the export script
- **Dependency Problems**: Make sure all Python packages are installed in the correct environment

## Support

For issues and feature requests, please open an issue in the [DeepStream-Yolo](https://github.com/marcoslucianops/DeepStream-Yolo) repository.
