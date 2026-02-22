import asyncio
import httpx
import base64
import time

BASE_URL = "http://localhost:8000"

async def test_flow():
    async with httpx.AsyncClient(timeout=30) as client:
        print("1. Checking health...")
        r = await client.get(f"{BASE_URL}/health")
        print(f"Health: {r.status_code}")

        print("\n2. Creating container session...")
        r = await client.post(f"{BASE_URL}/containers", json={})
        if r.status_code != 200:
            print(f"Failed to create container: {r.text}")
            return
            
        data = r.json()
        container_id = data["container_id"]
        print(f"Container created! ID: {container_id}")

        print("\n3. Checking container status...")
        r = await client.get(f"{BASE_URL}/containers/{container_id}")
        data = r.json()
        print(f"Status: {data['status']}, Uptime: {data['uptime_seconds']:.2f}s")
        
        print("\n4. Executing Python code...")
        python_code = "print('Hello from persistent Python session!')\nimport os\nwith open('/home/sandbox/test.txt', 'w') as f:\n  f.write('Python was here')"
        r = await client.post(f"{BASE_URL}/execute", json={
            "container_id": container_id,
            "language": "python",
            "code": python_code,
        })
        if r.status_code != 200:
             print(f"Failed Python execute: {r.text}")
        else:
             print("Python output:", r.json()["stdout"].strip())

        print("\n5. Executing Bash code (and reading the file Python wrote)...")
        bash_code = "echo 'Hello from Bash!'\nls -l /home/sandbox\ncat /home/sandbox/test.txt"
        r = await client.post(f"{BASE_URL}/execute", json={
            "container_id": container_id,
            "language": "bash",
            "code": bash_code,
        })
        if r.status_code != 200:
             print(f"Failed Bash execute: {r.text}")
        else:
             print("Bash output:\n", r.json()["stdout"].strip())
             
        print("\n6. Testing Input File Upload and Output Retrieval...")
        input_content = "This is a secret message uploaded from host."
        input_b64 = base64.b64encode(input_content.encode()).decode()
        
        script = """#!/bin/bash
echo "Reading input file:"
cat /home/sandbox/input.txt
echo "Writing output file:"
echo "Processed: $(cat /home/sandbox/input.txt)" > /tmp/output/result.txt
"""
        r = await client.post(f"{BASE_URL}/execute", json={
            "container_id": container_id,
            "language": "bash",
            "code": script,
            "files": [
                {"name": "input.txt", "content": input_b64}
            ]
        })
        if r.status_code != 200:
             print(f"Failed File Task: {r.text}")
        else:
             resp = r.json()
             print("Script stdout:\n", resp["stdout"].strip())
             files = resp["files"]
             print(f"Files returned: {len(files)}")
             if len(files) > 0:
                 decoded = base64.b64decode(files[0]["content"]).decode()
                 print(f"File '{files[0]['name']}' content: {decoded}")

        print("\n7. Deleting container session...")
        r = await client.delete(f"{BASE_URL}/containers/{container_id}")
        print("Delete response:", r.json())

        print("\n8. Checking container status again (should be 404)...")
        r = await client.get(f"{BASE_URL}/containers/{container_id}")
        print("Status check after delete:", r.status_code)

if __name__ == "__main__":
    asyncio.run(test_flow())
