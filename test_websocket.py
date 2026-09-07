import asyncio
import json
import websockets
import uuid

async def test_patient_stream():
    """测试患者康复流WebSocket"""
    patient_id = str(uuid.uuid4())  # 生成测试患者ID

    uri = f"ws://localhost:8001/rehab/stream/{patient_id}"  # 假设后端运行在localhost:8001

    try:
        async with websockets.connect(uri) as websocket:
            print(f"连接到患者流: {uri}")

            # 发送测试坐标数据
            test_coordinates = [
                {"x": 100, "y": 200, "z": 50},
                {"x": 110, "y": 210, "z": 55},
                {"x": 120, "y": 220, "z": 60}
            ]

            payload = {"coordinates": test_coordinates}
            await websocket.send(json.dumps(payload))
            print(f"发送坐标数据: {payload}")

            # 接收响应
            response = await websocket.recv()
            data = json.loads(response)
            print(f"接收响应: {data}")

            # 可以继续发送更多数据或等待
            await asyncio.sleep(1)

    except Exception as e:
        print(f"连接失败: {e}")

async def test_doctor_monitor():
    """测试医生监控WebSocket"""
    patient_id = str(uuid.uuid4())
    doctor_id = str(uuid.uuid4())  # 测试医生ID

    uri = f"ws://localhost:8001/doctor/monitor/{patient_id}?doctor_id={doctor_id}"

    try:
        async with websockets.connect(uri) as websocket:
            print(f"连接到医生监控: {uri}")

            # 接收订阅确认
            response = await websocket.recv()
            data = json.loads(response)
            print(f"接收订阅确认: {data}")

            # 等待患者数据（需要另一个协程发送数据）
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                data = json.loads(response)
                print(f"接收患者数据: {data}")
            except asyncio.TimeoutError:
                print("超时：未接收到患者数据")

    except Exception as e:
        print(f"连接失败: {e}")

async def main():
    print("开始WebSocket测试...")

    # 测试患者流
    await test_patient_stream()

    print("\n" + "="*50 + "\n")

    # 测试医生监控（需要同时运行患者流）
    await test_doctor_monitor()

if __name__ == "__main__":
    asyncio.run(main())