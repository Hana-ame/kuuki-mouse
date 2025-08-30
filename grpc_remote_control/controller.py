
import grpc
import time

# Import generated classes
from . import remote_control_pb2
from . import remote_control_pb2_grpc

class DummyController:
    def __init__(self, *kargs, **kwargs):
        pass
    def get_mouse_position(self):
        print("get_mouse_position")
        return {"x": -1, "y": -1}
    def move_mouse(self, x:int, y:int):
        """移动鼠标。"""
        print("x: ",x, "y: ",y)
    def click_mouse(self, button: str):
        """点击鼠标按钮。"""
        print(button)
        


class GrpcController:
    def __init__(self, addr='172.29.80.1:50051'):
        self.channel = grpc.insecure_channel(addr)
        self.stub = remote_control_pb2_grpc.RemoteControllerStub(self.channel)

    def get_mouse_position(self):
        """获取当前鼠标位置。"""
        try:    
            # Create an empty request message
            empty_request = remote_control_pb2.EmptyRequest()
            position_response = self.stub.GetMousePosition(empty_request)
            return {"x": position_response.x, "y": position_response.y}
        except grpc.RpcError as e:
            return {"x": -1, "y": -1}

    def move_mouse(self, x:int, y:int):
        """移动鼠标。"""
        move_request = remote_control_pb2.MousePosition(x=x, y=y)
        self.stub.MoveMouse(move_request)

    def click_mouse(self, button: str):
        """点击鼠标按钮。"""
        text_request = remote_control_pb2.ClickMouse(text=button)
        self.stub.ClickMouse(text_request)


def run():
    # Open a gRPC channel to the server
    # 'localhost:50051' is the server address and port
    with grpc.insecure_channel('172.29.80.1:50051') as channel:
        # Create a stub (client)
        stub = remote_control_pb2_grpc.RemoteControllerStub(channel)

        print("--- Remote Control Client Started ---")
        
        # --- 1. Get Mouse Position ---
        print("\n[1] Getting initial mouse position...")
        try:
            # Create an empty request message
            empty_request = remote_control_pb2.EmptyRequest()
            position_response = stub.GetMousePosition(empty_request)
            print(f"Client: Mouse is at ({position_response.x}, {position_response.y})")
        except grpc.RpcError as e:
            print(f"Error getting mouse position: {e.details()}")
            return # Exit if we can't connect

        time.sleep(2)

        # --- 2. Move Mouse ---
        target_x, target_y = 300, 400
        print(f"\n[2] Moving mouse to ({target_x}, {target_y})...")
        # Create a MousePosition request message
        move_request = remote_control_pb2.MousePosition(x=target_x, y=target_y)
        stub.MoveMouse(move_request)
        print("Client: Move command sent.")

        time.sleep(2)
        
        # Verify the new position
        print("Verifying new mouse position...")
        position_response = stub.GetMousePosition(empty_request)
        print(f"Client: Mouse is now at ({position_response.x}, {position_response.y})")

        time.sleep(2)

        # --- 3. Send Text ---
        text_to_send = "Hello from the gRPC client! This text is typed remotely."
        print(f"\n[3] Sending text to be typed: '{text_to_send}'")
        # Create a TextInput request message
        text_request = remote_control_pb2.TextInput(text=text_to_send)
        stub.SendText(text_request)
        print("Client: Text command sent. Check the server machine for the typed text.")

def test():
    controller = GrpcController()
    print(controller.get_mouse_position())
    controller.move_mouse(100, 200) # 相对位置。
    print(controller.get_mouse_position())

if __name__ == '__main__':
    #  py -m grpc_remote_control.controller
    test()