# grpc_remote_control/client.py

import grpc
import time

# Import generated classes
import remote_control_pb2
import remote_control_pb2_grpc

def run():
    # Open a gRPC channel to the server
    # 'localhost:50051' is the server address and port
    with grpc.insecure_channel('localhost:50051') as channel:
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


if __name__ == '__main__':
    run()