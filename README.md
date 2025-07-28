- first update the entire system using the built in .sh file.
```bash
root@inlights-B360M-HD3:/workspace# ls ../opt/nvidia/deepstream/deepstream-7.1/
    update_rtpmanager.sh
    user_additional_install.sh 
    user_deepstream_python_apps_install.sh 
```

- clone the Deepstream-Yolo github.
```bash
$ git clone https://github.com/marcoslucianops/DeepStream-Yolo.git
```
 
- clone the ultralytics repo and install requirements
```bash
git clone https://github.com/ultralytics/ultralytics.git
cd ultralytics
pip3 install -e .
pip3 install onnx onnxslim onnxruntime
```

- copy the **python3 export_yolo11.py -w yolo11s.pt --dynamic** to **ultralytics** directory.
```bash
$ cp DeepStream-Yolo/utils/export_yolo11.py ultralytics/
```

- use the **DeepStream-Yolo/utils/export_yolo11.py** file to convert your yolo model to **pt.onnx** format.
```bash
## conversion example
$ python3 export_yolo11.py -w yolo11s.pt --dynamic
```

- copy the path of **pt.onnx** and **labels.txt** file in **DeepStream-Yolo/config_infer_primary_yolo11.txt**
```bash
[property]
...
onnx-file=yolo11s.pt.onnx
...
num-detected-classes=80
...
parse-bbox-func-name=NvDsInferParseYolo
```

- Also modify the path inside **DeepStream-Yolo/deepstream_app_config.txt**.
```bash
...
[primary-gie]
...
config-file=config_infer_primary_yolo11.txt
```

- **[Important Step]** set the enironment variable
```bash
$ export CUDA_VER=12.6
```

- compile the library.
```bash
$ cd DeepStream-Yolo
$ make -C nvdsinfer_custom_impl_Yolo clean && make -C nvdsinfer_custom_impl_Yolo 
```

- now just change the paths in **DeepStream-Yolo/config_infer_primary_yolo11.txt**

- run the deepstream-app
```bash
$ deepstream-app -c deepstream_app_config.txt
```


