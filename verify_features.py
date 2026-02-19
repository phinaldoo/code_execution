#!/usr/bin/env python3
import json
import urllib.request
import urllib.error
import os
import time

BASE_URL = "http://localhost:8000"
API_KEY = "test-secret"

def execute(code, pip_packages=None, api_key=None):
    payload = {
        "code": code,
        "pip_packages": pip_packages or []
    }
    
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        
    req = urllib.request.Request(
        f"{BASE_URL}/execute",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": e.reason, "status": e.code}

def test_no_auth():
    print("Testing request without API key (when API_KEY is set in gateway)...")
    # This assumes we will run the gateway with API_KEY=test-secret
    res = execute("print('hello')")
    if res.get("status") == 401:
        print("✅ Correctly rejected with 401")
    else:
        print(f"❌ Expected 401, got {res}")

def test_bad_auth():
    print("Testing request with WRONG API key...")
    res = execute("print('hello')", api_key="wrong-key")
    if res.get("status") == 401:
        print("✅ Correctly rejected with 401")
    else:
        print(f"❌ Expected 401, got {res}")

def test_good_auth():
    print("Testing request with CORRECT API key...")
    res = execute("print('hello')", api_key=API_KEY)
    if "stdout" in res and res["stdout"].strip() == "hello":
        print("✅ Successfully authorized and executed")
    else:
        print(f"❌ Execution failed: {res}")

def test_pip_packages():
    print("Testing dynamic pip package installation (cowsay)...")
    code = "import cowsay; print(cowsay.get_output_string('cow', 'Moo!'))"
    res = execute(code, pip_packages=["cowsay"], api_key=API_KEY)
    
    if "stdout" in res and "Moo!" in res["stdout"]:
        print("✅ Pip package installed and used!")
        print(f"   Execution time: {res.get('execution_time')}s")
        if "install_time" in res:
             print(f"   Install time: {res['install_time']}s")
    else:
        print(f"❌ Pip test failed!")
        print(f"   Response: {json.dumps(res, indent=2)}")

if __name__ == "__main__":
    print("--- Verification of New Features ---")
    # Note: These tests require the gateway to be running with API_KEY=test-secret
    # and the sandbox image to be built.
    test_no_auth()
    test_bad_auth()
    test_good_auth()
    test_pip_packages()
