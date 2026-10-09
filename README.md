# Smart Airbag Helmet – Unified AI & Hardware Simulation

This project bridges a real-time **3D WebGL Cockpit Simulation** with a **Raspberry Pi Machine Learning Edge Pipeline** to evaluate and demo a smart motorcycle helmet airbag system.

## 🚀 Overview

The system simulates a motorcycle ride generating 9-axis sensor telemetry (accelerometer + gyroscope) in real-time. This data is streamed via WebSockets to a connected Raspberry Pi client. The Pi runs a 3-path Machine Learning detection pipeline that triggers a physical actuator (CO2 solenoid / warning buzzer) upon detecting a crash. 

### Core Layers:
*   **Layer 1:** AI Scenario Generation (LLM-driven telemetry generation).
*   **Layer 2:** WebGL 3D Cockpit (Three.js) & Unified WebSocket Server.
*   **Layer 3:** Raspberry Pi ML Edge Detector (Real-time inference).
*   **Layer 4:** Raspberry Pi Hardware Actuator (GPIO Airbag & Buzzer control).
*   **Layer 5:** Post-Crash Data Logging & Analytics.

---

## 🎮 Simulation UI Controls

The unified cockpit provides a "Simulation Control" panel to orchestrate both the 3D visualizer and the connected hardware. 

### Buttons

*   ▶ **Start Ride:** Initializes the simulation using a predefined deterministic telemetry dataset. It streams data in real-time to the browser 3D visualizer and to the Raspberry Pi over WebSockets.
*   💥 **Simulate Crash:** Triggers the crash event mid-ride. Its exact behavior depends on the active **Mode** (see below).
*   ↺ **Reset:** Stops playback, clears visual overlays, and resets the simulation state so you can start a fresh ride. *(Note: Resetting never forcefully re-arms a physically deployed airbag).*

### Operational Modes

The **Simulate Crash** button behaves differently based on which mode is selected. This allows for both crowd-pleasing physical demos and strict engineering benchmarks.

#### 1. FORCED MODE (Hardware Demo)
*   **Purpose:** For presentations and physical hardware demonstrations.
*   **What it does:** Clicking "Simulate Crash" instantly routes a validated hardware trigger through the server directly to the Raspberry Pi. The Pi bypasses its ML pipeline and immediately fires a warning buzzer pattern. 
*   **Safety:** To prevent accidental high-pressure deployments, the CO2 airbag output remains safely disarmed in this mode (only the warning buzzer activates).

#### 2. ML EVAL MODE (Algorithm Benchmarking)
*   **Purpose:** To test the intelligence and speed of the ML model.
*   **What it does:** Clicking "Simulate Crash" **does not** tell the Pi to deploy anything. Instead, it places a "Ground Truth Marker" on the server. The Raspberry Pi continues analyzing the raw sensor data completely independently.
*   **Benchmarking:** Only when the Pi's ML model independently recognizes the crash within the telemetry stream will it trigger a deployment. The UI will then compare the timestamps and display the **Detection Latency** (how quickly the AI realized a crash happened after the actual ground-truth event).

---

## 🛠️ How to Run

### 1. Start the Server
From the root directory, run the unified server:
```bash
python layer_2/server.py
```
*Open `http://localhost:5500` in your browser.*

### 2. Start the Raspberry Pi Client
In a new terminal (or on an actual Raspberry Pi), run the client:
```bash
python layer_pi/pi_ws_client.py
```
*(Append `--hardware` if running on a real Pi with GPIO connected).*

### 3. Run Automated Tests
A comprehensive test suite verifies server-side event routing, deduplication, and actuator safety interlocks (no hardware required).
```bash
python -m pytest tests/ -v
```

---

## 🔒 Safety Interlocks
*   **Airbag-in-a-box:** The `ActuatorEngine` enforces software-level safety constraints. `airbag_armed` defaults to `False` in all instances. 
*   Physical deployments require explicit hardware initialization, bypassing any external browser overrides.
*   Browser crash commands cannot send arbitrary GPIO control logic; they only route deterministic, pre-validated scenario IDs.
