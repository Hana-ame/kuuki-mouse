python -m grpc_tools.protoc \
  --python_out=grpc_remote_control \
  --grpc_python_out=grpc_remote_control \
  -I=grpc_remote_control \
  remote_control.proto