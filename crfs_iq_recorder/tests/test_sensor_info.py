from crfs_iq_recorder.sensor_info import build_sensor_info, parse_node_payload, parse_versions_payload


NODE = {
    "name": "rfeye100400",
    "nodeSoftwareVersion": "2.25-325",
    "hostVersion": "2.25-4823",
    "version": "2.25-3000",
}

VERSIONS = {
    "values": {
        "0-software-version": (
            "# Software Version\n<table style=\"margin-bottom:20px;float:left;\"><tbody>"
            "<tr><td style=\"padding:4px;\">Node Software</td>"
            "<td style=\"padding:4px;\">2.25-325</td></tr></tbody></table>"
        ),
        "1-hardware-info": (
            "# Radio Hardware and Firmware\n<table><tbody>"
            "<tr><td style=\"padding:4px;\">Device Type</td>"
            "<td style=\"padding:4px;\">R100-18</td></tr>"
            "<tr><td style=\"padding:4px;\">Radio Hardware</td>"
            "<td style=\"padding:4px;\">A08</td></tr></tbody></table>"
        ),
    }
}


def test_parse_node_name_and_firmware():
    serial, firmware = parse_node_payload(NODE)
    assert serial == "rfeye100400"
    assert firmware == "2.25-325"


def test_parse_versions_device_type_and_node_software():
    model, firmware = parse_versions_payload(VERSIONS)
    assert model == "R100-18"
    assert firmware == "2.25-325"


def test_build_prefers_versions_firmware_and_node_serial():
    info = build_sensor_info(
        host="192.0.2.12",
        node_payload=NODE,
        versions_payload=VERSIONS,
        status="Connected",
    )
    assert info.connected
    assert info.model == "R100-18"
    assert info.firmware == "2.25-325"
    assert info.serial == "rfeye100400"


def test_software_manager_fallback_when_node_json_missing():
    info = build_sensor_info(
        host="192.0.2.12",
        manager_payload={"label": "rfeye100400", "node_software_version": "2.25-325"},
        status="Connected",
    )
    assert info.serial == "rfeye100400"
    assert info.firmware == "2.25-325"
    assert info.model == "—"
