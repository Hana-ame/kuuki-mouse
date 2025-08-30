# grpc_remote_control/server.py

from concurrent import futures
import time
import grpc

# Import generated classes
from . import remote_control_pb2
from . import remote_control_pb2_grpc

# Import pynput for controlling mouse and keyboard
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController

# Create a class to define the server functions, derived from
# remote_control_pb2_grpc.RemoteControllerServicer
class RemoteControllerServicer(remote_control_pb2_grpc.RemoteControllerServicer):
    
    def __init__(self):
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        print("RemoteControllerServicer initialized.")

    # Method 1: Get Mouse Position
    def GetMousePosition(self, request, context):
        print("Server: Received GetMousePosition request.")
        x, y = self.mouse.position
        print(f"Server: Current mouse position is ({x}, {y}).")
        # Return a MousePosition message
        return remote_control_pb2.MousePosition(x=int(x), y=int(y))

    # Method 2: Move Mouse
    def MoveMouse(self, request, context):
        x, y = request.x, request.y
        print(f"Server: Received MoveMouse request to ({x}, {y}).")
        self.mouse.move(x, y)
        print(f"Server: Mouse moved to ({x}, {y}).")
        # Return an empty response
        return remote_control_pb2.EmptyResponse()
    
    # Method : Click Mouse
    def SendText(self, request, context):
        text_to_type = request.text
        print(f"Server: Received SendText request with text: '{text_to_type}'")
        if request.text == "click left":
            self.mouse.click(Button.left)
        elif request.text == "click right":
            self.mouse.click(Button.right)
        elif request.text == "click middle":
            self.mouse.click(Button.middle)
        elif request.text == "press left":
            self.mouse.press(Button.left)
        elif request.text == "press right":
            self.mouse.press(Button.right)
        elif request.text == "press middle":
            self.mouse.press(Button.middle)
        elif request.text == "release left":
            self.mouse.release(Button.left)
        elif request.text == "release right":
            self.mouse.release(Button.right)
        elif request.text == "release middle":
            self.mouse.release(Button.middle)
        else:
            scroll:str = request.text
            scroll.split(" ")
            if len(scroll) == 2 and scroll[0] == "scroll":
                try:
                    x_scroll = int(scroll[1])
                    self.mouse.scroll(x_scroll, 0)  # Scroll horizontally
                    print(f"Server: Scrolled horizontally by {x_scroll}.")
                except ValueError:
                    print("Server: Invalid scroll value, must be an integer.")
        print(f"Server: Typed text.")
        # Return an empty response
        return remote_control_pb2.EmptyResponse()
    
    # Method 3: Give Input Text
    def SendText(self, request, context):
        text_to_type = request.text
        print(f"Server: Received SendText request with text: '{text_to_type}'")
        self.keyboard.type(text_to_type)
        print(f"Server: Typed text.")
        # Return an empty response
        return remote_control_pb2.EmptyResponse()

def serve():
    # Create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    
    # Add the servicer to the server
    remote_control_pb2_grpc.add_RemoteControllerServicer_to_server(
        RemoteControllerServicer(), server
    )
    
    # Listen on port 50051
    port = "50051"
    server.add_insecure_port(f"[::]:{port}")
    
    # Start the server
    server.start()
    print(f"Server started. Listening on port {port}...")
    
    # Keep the server running
    try:
        while True:
            time.sleep(86400) # One day
    except KeyboardInterrupt:
        print("Stopping server...")
        server.stop(0)

if __name__ == '__main__':
    serve()