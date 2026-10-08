from pathlib import Path

import agent

FIXTURES = Path(__file__).parent / "fixtures"


def read(name):
    return (FIXTURES / name).read_text()


def test_parse_npu_910b():
    devices = agent.parse_npu_smi_info(read("npu-smi-910b.txt"))
    assert [d["index"] for d in devices] == [0, 1]
    first, second = devices
    assert first["npu_id"] == 0 and first["chip_id"] == 0
    assert first["model"] == "910B2"
    assert first["health"] == "OK"
    assert first["power_w"] == 107.3
    assert first["temperature"] == 43
    assert first["bus_id"] == "0000:C1:00.0"
    assert first["utilization"] == 0
    # 910B 取 HBM 用量
    assert first["memory_used_mb"] == 3862 and first["memory_total_mb"] == 65536
    assert [p["pid"] for p in first["processes"]] == [218169]
    assert first["processes"][0]["name"] == "ray_RayTrainW"
    assert first["processes"][0]["memory_mb"] == 558
    assert second["npu_id"] == 7
    assert second["processes"] == []


def test_parse_npu_310p():
    devices = agent.parse_npu_smi_info(read("npu-smi-310p.txt"))
    assert len(devices) == 1
    device = devices[0]
    assert device["index"] == 0
    assert device["model"] == "310P3"
    assert device["power_w"] is None  # NA
    assert device["temperature"] == 68
    assert device["memory_used_mb"] == 1630 and device["memory_total_mb"] == 44280


def test_parse_nvidia():
    devices = agent.parse_nvidia_gpus(read("nvidia-smi-gpu.csv"), read("nvidia-smi-apps.csv"))
    assert [d["index"] for d in devices] == [0, 1]
    first, second = devices
    assert first["model"] == "NVIDIA A100-SXM4-80GB"
    assert first["memory_used_mb"] == 1024 and first["memory_total_mb"] == 81920
    assert first["utilization"] == 35
    assert first["power_w"] == 88.52
    assert first["bus_id"] == "00000000:07:00.0"
    assert [p["pid"] for p in first["processes"]] == [4242]
    assert second["power_w"] is None  # [N/A]
    assert second["processes"] == []
